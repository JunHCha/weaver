"""Materialize approved event summaries and their extracted Threads."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, JsonValue

from weaver.context import ContextDictionary, Kind
from weaver.courier.atomic import write_json_atomic
from weaver.llm.types import LlmClient
from weaver.schema.v2 import (
    EventSummary,
    ReviewPackage,
    ThreadDraft,
    ThreadSink,
    Verdict,
)


class Correction(BaseModel):
    """One correction explicitly chosen by the reviewer."""

    observed: str
    canonical: str
    kind: Kind


class ExtractedThread(BaseModel):
    """Model-provided fact, with provenance supplied by the server."""

    kind: Literal["요청", "결정", "일정", "기록"]
    content: str
    occurred_at: datetime
    tags: list[str]
    field: Literal["deadline", "schedule", "assignees", "status"] | None
    value: JsonValue
    direction: Literal["받음", "보냄"] | None
    resolves: str | None
    retracts: str | None


class ApprovedResponse(BaseModel):
    """Final answer-applied text, reviewer corrections, and extracted facts."""

    body: str
    corrections: list[Correction]
    threads: list[ExtractedThread]


class SummaryReview:
    """Approve one package using the model and a ThreadSink boundary."""

    def __init__(
        self,
        llm: LlmClient,
        context: ContextDictionary,
        sink: ThreadSink,
        archive: Path,
    ) -> None:
        self.llm = llm
        self.context = context
        self.sink = sink
        self.archive = archive

    def approve(
        self,
        package: ReviewPackage,
        script: str,
        verdict: Verdict,
        *,
        now: datetime | None = None,
        replaces: str | None = None,
    ) -> EventSummary:
        """Apply answers, archive a content-addressed summary and append its facts."""
        if verdict.action != "approve" or (package.package_id, package.revision) != (
            verdict.package_id,
            verdict.revision,
        ):
            raise ValueError("verdict does not approve this revision")
        if len(verdict.answers) != len(package.questions) or any(
            answer not in question.choices
            for answer, question in zip(verdict.answers, package.questions, strict=True)
        ):
            raise ValueError("answers must select one choice for each question")
        timestamp = now or datetime.now(UTC)
        response = self.llm.generate_json(
            "loom.approve",
            "검토 답을 문서에 반영하고 승인된 사실과 명시된 이름 교정만 추출한다.",
            json.dumps(
                {
                    "body": package.draft_body,
                    "questions": [q.model_dump() for q in package.questions],
                    "answers": verdict.answers,
                },
                ensure_ascii=False,
            ),
            ApprovedResponse,
        )
        fields = {
            "schema_version": 2,
            "source_id": package.source_id,
            "script": script,
            "body": response.body,
            "replaces": replaces,
            "created_at": timestamp.isoformat(),
        }
        digest = hashlib.sha256(
            json.dumps(
                fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        summary = EventSummary.model_validate({"summary_id": digest, **fields})
        drafts = [
            ThreadDraft(
                thread_id=f"thr_{uuid4().hex}",
                **thread.model_dump(),
                recorded_at=timestamp,
                source_id=package.source_id,
                actor="ai_approved",
                correction=None,
            )
            for thread in response.threads
        ]
        for correction in response.corrections:
            if (
                correction.canonical not in verdict.answers
                or correction.observed not in script
            ):
                raise ValueError("correction was not selected or observed")
        target = self.archive / digest[:2] / f"{digest}.json"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if EventSummary.model_validate_json(target.read_bytes()) != summary:
                raise ValueError("summary archive conflict")
        else:
            write_json_atomic(target, summary)
        self.sink.append(drafts)
        for correction in response.corrections:
            self.context.learn(
                correction.observed, correction.canonical, correction.kind
            )
        return summary
