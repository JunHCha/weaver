"""Materialize approved event summaries and their extracted Threads."""

import difflib
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NotRequired, Self, TypedDict
from zoneinfo import ZoneInfo

from pydantic import BaseModel, TypeAdapter, model_validator

from weaver.context import ContextDictionary, Kind
from weaver.courier.atomic import write_bytes_atomic, write_json_atomic
from weaver.llm.types import LlmClient
from weaver.loom.summary_patch import Patch, apply_patches
from weaver.loom.summary_state import SummaryState
from weaver.schema import (
    EventSummary,
    ReviewPackage,
    ScheduleValue,
    ThreadDraft,
    ThreadSink,
    Verdict,
)


class Correction(BaseModel):
    """One correction explicitly chosen by the reviewer."""

    observed: str
    canonical: str
    kind: Kind


class ExtractedSchedule(TypedDict):
    """Raw local times; SummaryReview supplies the configured timezone."""

    start: str
    end: NotRequired[str | None]
    timezone: NotRequired[str | None]


class AssigneeChange(TypedDict):
    """ADR-033 set operation, never a replacement list."""

    add: list[str]
    remove: list[str]


class ExtractedThread(BaseModel):
    """Model-provided fact, with provenance supplied by the server."""

    kind: Literal["요청", "결정", "일정", "기록"]
    content: str
    occurred_at: datetime
    tags: list[str]
    field: Literal["deadline", "schedule", "assignees", "status"] | None
    value: str | ExtractedSchedule | AssigneeChange | None
    direction: Literal["받음", "보냄"] | None
    resolves: str | None
    retracts: str | None
    blocked_by_index: int | None = None

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if self.value is not None:
            if self.field == "schedule":
                TypeAdapter(ExtractedSchedule).validate_python(self.value)
            elif self.field == "assignees":
                TypeAdapter(AssigneeChange).validate_python(self.value)
            elif self.field in ("deadline", "status"):
                if not isinstance(self.value, str):
                    raise ValueError("deadline and status values must be strings")
            else:
                raise ValueError("value requires a field")
        return self


class ApprovedResponse(BaseModel):
    """Final answer-applied text, reviewer corrections, and extracted facts."""

    body: str
    corrections: list[Correction]
    threads: list[ExtractedThread]


class FeedbackResponse(BaseModel):
    """Targeted replacements proposed for reviewer feedback."""

    patches: list[Patch]


class Prepared(BaseModel):
    """Stable side-effect payload retained across retries."""

    summary: EventSummary | None = None
    package: ReviewPackage | None = None
    diff: str = ""
    applied_at: datetime | None = None
    threads: list[ThreadDraft] = []
    corrections: list[Correction] = []


class SummaryReview:
    """Approve one package using the model and a ThreadSink boundary."""

    def __init__(
        self,
        llm: LlmClient,
        context: ContextDictionary,
        sink: ThreadSink,
        archive: Path,
        *,
        state_root: Path,
        proposals: Path | None = None,
        timezone: str = "Asia/Seoul",
    ) -> None:
        self.timezone = ZoneInfo(timezone).key
        self.llm = llm
        self.context = context
        self.sink = sink
        self.archive = archive
        self.state = SummaryState(state_root)
        self.proposals = proposals or state_root / "proposals"

    def approve(
        self,
        package: ReviewPackage,
        script: str,
        verdict: Verdict,
        *,
        now: datetime | None = None,
        replaces: str | None = None,
    ) -> EventSummary:
        """Apply an approval and return its archived summary."""
        if verdict.action != "approve":
            raise ValueError("verdict does not approve this revision")
        result = self.apply(package, script, verdict, now=now, replaces=replaces)
        assert isinstance(result, EventSummary)
        return result

    def apply(
        self,
        package: ReviewPackage,
        script: str,
        verdict: Verdict,
        *,
        now: datetime | None = None,
        replaces: str | None = None,
    ) -> EventSummary | ReviewPackage | None:
        """Apply one verdict, replaying durable side effects safely after interruption."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", package.package_id):
            raise ValueError("invalid package id")
        if (package.package_id, package.revision) != (
            verdict.package_id,
            verdict.revision,
        ):
            raise ValueError("verdict does not match this revision")
        if verdict.action == "approve" and len(verdict.answers) != len(
            package.questions
        ):
            raise ValueError(
                "answers must include one nonblank answer for each question"
            )
        if len(verdict.answers) > len(package.questions):
            raise ValueError("too many answers")
        with self.state.lock():
            self.state.save_package(package, script)
            with self.state.connect() as db:
                row = db.execute(
                    "SELECT verdict, prepared, stage FROM reviews WHERE id=? AND revision=?",
                    (package.package_id, package.revision),
                ).fetchone()
            if row is not None:
                if Verdict.model_validate_json(row[0]) != verdict:
                    raise ValueError("conflicting verdict")
                prepared = Prepared.model_validate_json(row[1])
                if row[2] == "APPLIED":
                    return prepared.summary or prepared.package
            else:
                timestamp = now or datetime.now(UTC)
                prepared = self._prepare_action(
                    package, script, verdict, timestamp, replaces
                )
                prepared.applied_at = timestamp
                with self.state.connect() as db:
                    db.execute(
                        "INSERT INTO reviews VALUES (?, ?, ?, ?, 'PREPARED')",
                        (
                            package.package_id,
                            package.revision,
                            verdict.model_dump_json(),
                            prepared.model_dump_json(),
                        ),
                    )
            if prepared.summary is not None:
                target = (
                    self.archive
                    / prepared.summary.summary_id[:2]
                    / f"{prepared.summary.summary_id}.json"
                )
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if target.exists():
                    if (
                        EventSummary.model_validate_json(target.read_bytes())
                        != prepared.summary
                    ):
                        raise ValueError("summary archive conflict")
                else:
                    write_json_atomic(target, prepared.summary)
            if prepared.threads:
                self.sink.append(prepared.threads)
            for correction in prepared.corrections:
                self.context.learn(
                    correction.observed, correction.canonical, correction.kind
                )
            self.proposals.mkdir(parents=True, exist_ok=True)
            finished = package.model_copy(
                update={
                    "stage": "반영",
                    "stage_times": package.stage_times
                    | {"반영": prepared.applied_at or datetime.now(UTC)},
                }
            )
            # Use the journaled time for deterministic publication on replay.
            if prepared.package is not None:
                self.state.save_package(prepared.package, script)
                write_bytes_atomic(
                    self.proposals
                    / f"{package.package_id}.r{prepared.package.revision}.diff",
                    prepared.diff.encode(),
                )
                write_json_atomic(
                    self.proposals
                    / f"{package.package_id}.r{prepared.package.revision}.json",
                    prepared.package,
                )
            write_json_atomic(
                self.proposals / f"{package.package_id}.r{package.revision}.json",
                finished,
            )
            with self.state.connect() as db:
                db.execute(
                    "UPDATE reviews SET stage='APPLIED' WHERE id=? AND revision=?",
                    (package.package_id, package.revision),
                )
            return prepared.summary or prepared.package

    def _prepare_action(
        self,
        package: ReviewPackage,
        script: str,
        verdict: Verdict,
        now: datetime | None,
        replaces: str | None,
    ) -> Prepared:
        timestamp = now or datetime.now(UTC)
        if verdict.action == "feedback":
            base_body = verdict.edited_body or package.draft_body
            response = self.llm.generate_json(
                "loom.feedback",
                "요청한 변경만 원문 부분 교체 목록으로 작성한다.",
                json.dumps(
                    {
                        "body": base_body,
                        "feedback": verdict.feedback,
                        "questions": [q.model_dump() for q in package.questions],
                        "answers": verdict.answers,
                    },
                    ensure_ascii=False,
                ),
                FeedbackResponse,
            )
            body = apply_patches(base_body, response.patches)
            revised = package.model_copy(
                update={
                    "revision": package.revision + 1,
                    "draft_body": body,
                    "stage": "검토",
                    "stage_times": package.stage_times
                    | {"정리": timestamp, "질문": timestamp, "검토": timestamp},
                }
            )
            diff = "\n".join(
                difflib.unified_diff(
                    package.draft_body.splitlines(),
                    body.splitlines(),
                    fromfile="previous",
                    tofile="revised",
                )
            )
            return Prepared(package=revised, diff=diff + "\n" if diff else "")
        if verdict.action == "reject":
            key = hashlib.sha256(
                f"{package.package_id}:{package.revision}:reject".encode()
            ).hexdigest()
            return Prepared(
                threads=[
                    ThreadDraft(
                        thread_id=f"thr_{key}",
                        kind="거절",
                        content=None,
                        occurred_at=timestamp,
                        recorded_at=timestamp,
                        tags=[],
                        field=None,
                        value=None,
                        source_id=package.source_id,
                        actor="human",
                        direction=None,
                        resolves=None,
                        retracts=None,
                        correction=None,
                    )
                ]
            )
        return self._prepare(package, script, verdict, timestamp, replaces)

    def _prepare(
        self,
        package: ReviewPackage,
        script: str,
        verdict: Verdict,
        now: datetime | None,
        replaces: str | None,
    ) -> "Prepared":
        timestamp = now or datetime.now(UTC)
        response = self.llm.generate_json(
            "loom.approve",
            (
                "사람이 작성한 본문에 검토 답과 추가 피드백을 반영하고 사실만 추출한다."
                if verdict.action == "edit"
                else "검토 답과 추가 피드백을 문서에 반영하고 승인된 사실과 명시된 이름 교정만 추출한다."
            )
            + (
                " 일정은 field=schedule, value={start,end,timezone}으로 추출한다."
                " 시각은 ISO 8601로 쓰고 종료나 시간대가 없으면 null로 둔다."
                " 문서에 요청 완료가 일정의 선행 조건으로 명시된 경우에만"
                " blocked_by_index에 같은 응답 threads 배열의 요청 인덱스(0부터)를 쓴다."
                " 의존 관계를 추측하지 말고 없거나 불확실하면 null로 둔다."
            ),
            json.dumps(
                {
                    "timezone": self.timezone,
                    "body": verdict.edited_body
                    if verdict.action == "edit"
                    else package.draft_body,
                    "questions": [q.model_dump() for q in package.questions],
                    "answers": verdict.answers,
                    "feedback": verdict.feedback,
                },
                ensure_ascii=False,
            ),
            ApprovedResponse,
        )
        fields = {
            "schema_version": 2,
            "source_id": package.source_id,
            "script": script,
            "body": verdict.edited_body
            if verdict.action == "edit" and verdict.feedback is None
            else response.body,
            "replaces": replaces,
            "created_at": timestamp.isoformat(),
        }
        digest = hashlib.sha256(
            json.dumps(
                fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        summary = EventSummary.model_validate({"summary_id": digest, **fields})
        ids = [
            "thr_" + hashlib.sha256(f"{digest}:{index}".encode()).hexdigest()
            for index in range(len(response.threads))
        ]
        drafts: list[ThreadDraft] = []
        for thread in response.threads:
            data = thread.model_dump(exclude={"blocked_by_index"})
            if thread.field == "schedule" and thread.value is not None:
                if not isinstance(thread.value, dict):
                    raise ValueError("schedule value must be an object")
                value = dict(thread.value)
                zone_name = value.get("timezone")
                if zone_name is None:
                    zone_name = self.timezone
                if not isinstance(zone_name, str):
                    raise ValueError("schedule timezone must be a string")
                zone = ZoneInfo(zone_name)
                value["timezone"] = zone.key
                for key in ("start", "end"):
                    raw = value.get(key)
                    if raw is not None:
                        if not isinstance(raw, str):
                            raise ValueError("schedule time must be a string")
                        dt = datetime.fromisoformat(raw)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=zone)
                            if dt.astimezone(UTC).astimezone(zone).replace(
                                tzinfo=None
                            ) != dt.replace(tzinfo=None):
                                raise ValueError("nonexistent local schedule time")
                        value[key] = dt.isoformat()
                data["value"] = ScheduleValue.model_validate(value).model_dump(
                    mode="json"
                )
            index = thread.blocked_by_index
            if index is not None and (
                thread.kind != "일정"
                or index < 0
                or index >= len(response.threads)
                or response.threads[index].kind != "요청"
            ):
                raise ValueError(
                    "blocked_by_index must reference a request in this batch"
                )
            drafts.append(
                ThreadDraft(
                    thread_id=ids[len(drafts)],
                    **data,
                    blocked_by=ids[index] if index is not None else None,
                    recorded_at=timestamp,
                    source_id=package.source_id,
                    actor="human" if verdict.action == "edit" else "ai_approved",
                    correction=None,
                )
            )
        corrections = response.corrections if verdict.action == "approve" else []
        for correction in corrections:
            if (
                correction.canonical not in verdict.answers
                or correction.observed not in script
            ):
                raise ValueError("correction was not selected or observed")
        return Prepared(summary=summary, threads=drafts, corrections=corrections)
