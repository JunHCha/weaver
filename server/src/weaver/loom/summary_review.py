"""Materialize approved event summaries and their extracted Threads."""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, Protocol, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from weaver.context import ContextDictionary, DictionarySnapshot, Kind, _key
from weaver.courier.atomic import write_bytes_atomic, write_json_atomic
from weaver.fabric import ThreadQuery
from weaver.llm.types import LlmClient
from weaver.loom.summary_state import SummaryState
from weaver.loom.tagging import (
    FACT_RULES,
    MAX_NEW_ENTRIES,
    TAG_RULES,
    NewEntry,
    business_tags,
    counterpart_name,
    proposals,
    tag_context,
    tag_snapshot,
)
from weaver.loom.tagging import TAG_CONTEXT_TOKEN_BUDGET as TAG_CONTEXT_TOKEN_BUDGET
from weaver.schema import (
    EventSummary,
    ReviewPackage,
    ScheduleValue,
    Thread,
    ThreadDraft,
    ThreadSink,
    Verdict,
)
from weaver.schema.models import NonEmpty, Timestamp

logger = logging.getLogger(__name__)


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


def _literal_enums(schema: dict[str, Any]) -> None:
    # Anthropic 0.125 drops const into a description, but preserves enum.
    # Keep single-value kind/field literals enforceable after transformation.
    for property_schema in schema["properties"].values():
        if "const" in property_schema:
            property_schema["enum"] = [property_schema.pop("const")]


class _ExtractedThread[K, D, R, B, F, V](BaseModel):
    """Flat response contract specialized by both kind and field.

    Plain unions deliberately emit anyOf, which survives Anthropic's schema
    transform; discriminator/if/then constraints are not needed.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra=_literal_enums)

    kind: K
    content: NonEmpty
    occurred_at: Timestamp
    tags: list[NonEmpty]
    counterpart: NonEmpty | None = None
    field: F
    value: V
    direction: D
    resolves: R
    retracts: NonEmpty | None
    blocked_by_index: B | None = None
    updates: NonEmpty | None = None


type _FieldThread[K, D, R, B] = (
    _ExtractedThread[K, D, R, B, None, None]
    | _ExtractedThread[K, D, R, B, Literal["deadline", "status"], str | None]
    | _ExtractedThread[K, D, R, B, Literal["schedule"], ExtractedSchedule | None]
    | _ExtractedThread[K, D, R, B, Literal["assignees"], AssigneeChange | None]
)
type ExtractedThread = (
    _FieldThread[Literal["요청"], Literal["받음", "보냄"], None, None]
    | _FieldThread[Literal["결정"], None, NonEmpty | None, None]
    | _FieldThread[Literal["일정"], None, None, Annotated[int, Field(ge=0)] | None]
    | _FieldThread[Literal["기록"], None, None, None]
)


class ApprovedResponse(BaseModel):
    """Final answer-applied text, reviewer corrections, and extracted facts."""

    body: str
    new_entries: list[NewEntry] = Field(
        default_factory=list, max_length=MAX_NEW_ENTRIES
    )
    corrections: list[Correction]
    threads: list[ExtractedThread]


class Prepared(BaseModel):
    """Stable side-effect payload retained across retries."""

    summary: EventSummary | None = None
    package: ReviewPackage | None = None
    diff: str = ""
    applied_at: datetime | None = None
    threads: list[ThreadDraft] = []
    corrections: list[Correction] = []
    new_entries: list[NewEntry] = []


class DictionaryReader(Protocol):
    def snapshot(self) -> DictionarySnapshot: ...


class ApprovalExtractor:
    """Prepare the current approval extraction without any persistent writes."""

    def __init__(
        self,
        llm: LlmClient,
        context: DictionaryReader,
        sink: ThreadSink,
        *,
        timezone: str = "Asia/Seoul",
        contact_name: str | None = None,
    ) -> None:
        self.contact_name = contact_name
        self.llm, self.context, self.sink = llm, context, sink
        self.timezone = ZoneInfo(timezone).key

    def prepare(
        self,
        package: ReviewPackage,
        script: str,
        verdict: Verdict,
        *,
        now: datetime,
    ) -> Prepared:
        if verdict.action not in ("approve", "edit"):
            raise ValueError("only approved reviews can be extracted")
        return self._prepare(package, script, verdict, now, None)

    def _schedules(self) -> tuple[set[str], list[Thread]]:
        """Schedule Threads already in Fabric: every valid link target, and the
        ones still standing (shown to the model). A sink that cannot be read
        has none, so no link can be made.
        """
        entries = getattr(self.sink, "entries", None)
        if not callable(entries):
            return set(), []
        threads: list[Thread] = entries(ThreadQuery())
        retracted = {t.retracts for t in threads if t.retracts}
        schedules = [t for t in threads if t.kind == "일정" and t.value is not None]
        active = [t for t in schedules if t.thread_id not in retracted]
        active.sort(key=lambda t: t.seq)
        return {t.thread_id for t in schedules}, active[-50:]

    def _prepare(
        self,
        package: ReviewPackage,
        script: str,
        verdict: Verdict,
        now: datetime | None,
        replaces: str | None,
    ) -> "Prepared":
        timestamp = now or datetime.now(UTC)
        known, existing_active = self._schedules()
        full_dictionary = self.context.snapshot()
        dictionary = tag_snapshot(full_dictionary)
        entries = getattr(self.sink, "entries", None)
        all_threads: list[Thread] = entries(ThreadQuery()) if callable(entries) else []
        hints = tag_context(full_dictionary, script, all_threads)
        retracted = {t.retracts for t in all_threads if t.retracts}
        active = [
            t
            for t in all_threads
            if t.thread_id not in retracted and t.retracts is None
        ]
        resolved = {t.resolves for t in active if t.resolves}
        requests = [
            t for t in active if t.kind == "요청" and t.thread_id not in resolved
        ]
        request_ids = {t.thread_id for t in requests}
        related = {
            e.name
            for e in dictionary.entries
            if any(
                _key(label) in _key(script + package.draft_body)
                for label in (e.name, *e.aliases)
            )
        }
        requests.sort(key=lambda t: (-len(set(t.tags) & related), -t.seq, t.thread_id))
        open_requests: list[dict[str, Any]] = []
        cost = 2
        for request in requests:
            hint = {
                "id": request.thread_id,
                "content": request.content,
                "tags": request.tags,
                "direction": request.direction,
                "occurred_at": request.occurred_at.isoformat(),
            }
            size = len(json.dumps(hint, ensure_ascii=False).encode()) + 2
            if len(open_requests) < 50 and cost + size <= 8000:
                open_requests.append(hint)
                cost += size
        response = self.llm.generate_json(
            "loom.approve",
            (
                "사람이 작성한 본문에 검토 답과 추가 피드백을 반영하고 사실만 추출한다."
                if verdict.action == "edit"
                else "검토 답과 추가 피드백을 문서에 반영하고 승인된 사실과 명시된 이름 교정만 추출한다."
            )
            + (
                "\n"
                + FACT_RULES
                + "\n"
                + TAG_RULES
                + "\n기록 주인(화자 본인)은 태그하지 않는다. contact_name은 통화 상대다."
                + " counterpart는 업무를 주고받은 상대 한 명/조직의 표준 이름 또는 null이다."
                " 통화 상대(contact_name), 받음 요청의 요청자, 보냄 요청의 수신자를 쓴다."
                " 목록으로 쓰지 말고 불명확하면 null이다."
                + (
                    " 오인식은 corrections(observed → canonical)로만 제안한다."
                    " observed는 원문에 실제로 있는 표기이고 canonical은 검토 답 하나와 정확히 일치해야 한다."
                    if verdict.action == "approve"
                    else " edit에서는 corrections를 비운다."
                )
                + (
                    " 일정은 field=schedule, value={start,end,timezone}으로 추출한다."
                    " 시각은 ISO 8601로 쓰고 종료나 시간대가 없으면 null로 둔다."
                    " 문서에 요청 완료가 일정의 선행 조건으로 명시된 경우에만"
                    " blocked_by_index에 같은 응답 threads 배열의 요청 인덱스(0부터)를 쓴다."
                    " 의존 관계를 추측하지 말고 없거나 불확실하면 null로 둔다."
                    " 요청에만 direction을 받음/보냄으로 쓰고 나머지는 null로 둔다."
                    " open_requests는 활성 미해결 요청이다. 문서가 해당 요청의 완료·응답·취소를"
                    " 명시한 결정에만 resolves로 그 id를 쓴다. 유사성만으로 연결하지 않는다."
                    " 그 외 resolves는 null로 둔다."
                    " field가 없으면 value도 null이다. deadline/status 값은 문자열,"
                    " assignees 값은 {add: [이름], remove: [이름]} 집합 연산이다."
                    " content, tags 항목, resolves/retracts ID는 공백뿐이면 안 된다."
                    " occurred_at은 시간대를 포함한 RFC 3339 시각이다."
                    " 일정 start는 유효한 ISO 8601 시각이며 end는 start보다 늦어야 한다."
                    " timezone은 유효한 IANA 이름이나 null이고 null은 설정 시간대를 쓴다."
                    " 해당 시간대에 존재하지 않는 현지 시각을 만들지 않는다."
                    " blocked_by_index는 반드시 이 배열 범위 안의 요청을 가리킨다."
                    " existing_schedules는 이미 승인된 일정이다. 문서가 그중 하나의 변경"
                    "(시각 이동 등)이라고 명시한 일정에만 updates에 그 id를 쓴다."
                    " 제목이나 태그가 비슷하다는 이유로 연결하지 말고, 새 일정이거나"
                    " 불확실하면 null로 둔다. 일정이 아닌 Thread의 updates는 항상 null이다."
                    " 태그정정/거절 Thread는 추출하지 않는다. correction은 서버가 null로 둔다."
                )
            ),
            json.dumps(
                {
                    **hints,
                    "contact_name": self.contact_name,
                    "timezone": self.timezone,
                    "open_requests": open_requests,
                    "existing_schedules": [
                        {
                            "id": t.thread_id,
                            "title": t.content,
                            "value": t.value,
                            "tags": t.tags,
                        }
                        for t in existing_active
                    ],
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
        # Validate proposals without mutating the real dictionary until PREPARED
        # is durable. First declaration owns a spelling; existing names win.
        new_entries, proposed_names = proposals(full_dictionary, response.new_entries)
        drafts: list[ThreadDraft] = []
        for thread in response.threads:
            data = thread.model_dump(exclude={"blocked_by_index", "updates"})
            data["tags"] = business_tags(
                dictionary, new_entries, proposed_names, thread.tags
            )
            data["counterpart"] = counterpart_name(
                dictionary, new_entries, proposed_names, thread.counterpart
            )
            if (
                data["counterpart"] is not None
                and data["counterpart"] not in data["tags"]
            ):
                data["tags"].append(data["counterpart"])
            if thread.resolves is not None and thread.resolves not in request_ids:
                logger.warning("dropped invalid request resolution")
                data["resolves"] = None
            updates = thread.updates
            if updates is not None and (
                thread.kind != "일정" or thread.value is None or updates not in known
            ):
                # An unfounded link is dropped; the schedule stays a new event.
                logger.warning("dropped unfounded schedule update link")
                updates = None
            if thread.field == "schedule" and thread.value is not None:
                if not isinstance(thread.value, dict):
                    raise ValueError("schedule value must be an object")
                value = dict(thread.value)
                zone_name = value.get("timezone")
                if zone_name is None:
                    zone_name = self.timezone
                if not isinstance(zone_name, str):
                    raise ValueError("schedule timezone must be a string")
                try:
                    zone = ZoneInfo(zone_name)
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError("unknown timezone") from None
                value["timezone"] = zone.key
                for key in ("start", "end"):
                    raw = value.get(key)
                    if raw is not None:
                        if not isinstance(raw, str):
                            raise ValueError("schedule time must be a string")
                        try:
                            dt = datetime.fromisoformat(raw)
                        except ValueError:
                            raise ValueError("invalid schedule time") from None
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
                    updates=updates,
                    recorded_at=timestamp,
                    source_id=package.source_id,
                    actor="human" if verdict.action == "edit" else "ai_approved",
                    correction=None,
                )
            )
        proposed = response.corrections if verdict.action == "approve" else []
        # Corrections only teach the context dictionary. An unverifiable one is
        # dropped instead of failing the approval; the dictionary stays protected.
        corrections = [
            c
            for c in proposed
            if c.canonical in verdict.answers and c.observed in script
        ]
        if len(corrections) < len(proposed):
            logger.warning(
                "dropped unverified corrections: %d of %d",
                len(proposed) - len(corrections),
                len(proposed),
            )
        used = {tag for draft in drafts for tag in draft.tags}
        hinted_names = {hint["name"] for hint in hints["dictionary"]}
        body_key = _key(summary.body)
        missing = sum(
            entry.kind in ("person", "organization")
            and _key(entry.name) in body_key
            and entry.name not in used
            for entry in dictionary.entries
            if entry.name in hinted_names
        )
        if missing:
            logger.warning("approved body has untagged dictionary names: %d", missing)
        new_entries = [entry for entry in new_entries if entry.name in used]
        return Prepared(
            summary=summary,
            threads=drafts,
            corrections=corrections,
            new_entries=new_entries,
        )


class SummaryReview(ApprovalExtractor):
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
        super().__init__(llm, context, sink, timezone=timezone)
        self.context: ContextDictionary = context
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
                if json.loads(row[0]) != verdict.model_dump(mode="json"):
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
            for entry in prepared.new_entries:
                self.context.add_entry(
                    entry.name,
                    entry.kind,
                    tuple(entry.aliases),
                    human_edited=False,
                    drop_conflicting_aliases=True,
                )
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
        self.contact_name = self.state.contact_name(
            package.package_id, package.source_id
        )
        return self._prepare(package, script, verdict, timestamp, replaces)
