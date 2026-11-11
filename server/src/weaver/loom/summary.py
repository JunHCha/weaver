"""Draft one review document for each source."""

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from weaver.context import ContextDictionary
from weaver.courier.atomic import write_json_atomic
from weaver.llm.types import LlmClient
from weaver.loom.summary_state import SummaryState
from weaver.schema import Question, ReviewPackage


class DraftResponse(BaseModel):
    """Unbounded model output; the wire package caps questions separately."""

    title: str = ""
    script: str
    body: str
    questions: list[Question]


class SummaryDrafts:
    """Generate and publish a review package from a complete Script."""

    def __init__(
        self,
        llm: LlmClient,
        context: ContextDictionary,
        proposals: Path,
        *,
        state_root: Path,
        revision_threshold: int = 30000,
    ) -> None:
        if revision_threshold < 1:
            raise ValueError("revision_threshold must be positive")
        self.revision_threshold = revision_threshold
        self.llm = llm
        self.context = context
        self.proposals = proposals
        self.state = SummaryState(state_root)

    def create(
        self,
        source_id: str,
        script: str,
        contact_name: str | None = None,
        *,
        source_type: str = "",
        now: datetime | None = None,
        package_id: str | None = None,
    ) -> tuple[ReviewPackage, str]:
        """Return the package and revised Script after atomically publishing it."""
        if not script.strip():
            raise ValueError("script must not be empty")
        package_id = package_id or f"pkg_{uuid4().hex}"
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", package_id):
            raise ValueError("invalid package id")
        source_text = script
        candidates = self.context.candidates(script, contact_name)
        if len(script) > self.revision_threshold:
            script = self.llm.generate_text(
                "loom.polish",
                "녹취를 맥락에 따라 윤문한다.",
                json.dumps(
                    {
                        "script": script,
                        "contact_name": contact_name,
                        "context": [asdict(entry) for entry in candidates],
                    },
                    ensure_ascii=False,
                ),
            )
            if not script.strip():
                raise ValueError("revised script must not be empty")
        response = self.llm.generate_json(
            "loom.summary",
            (
                "녹취를 윤문하고 요약과 확신이 낮은 질문을 작성한다. "
                "title에는 내용의 주제를 짧게 쓰고, 각 질문의 source_excerpt에는 "
                "source_text에서 질문과 관련된 원문을 그대로 인용한다."
            ),
            json.dumps(
                {
                    "script": script,
                    "source_text": source_text,
                    "contact_name": contact_name,
                    "context": [asdict(entry) for entry in candidates],
                },
                ensure_ascii=False,
            ),
            DraftResponse,
        )
        body = response.body
        if len(response.questions) > 3:
            body += "\n\n확인 필요\n" + "\n".join(
                f"- {q.question}" for q in response.questions[3:]
            )
        timestamp = now or datetime.now(UTC)
        package = ReviewPackage(
            schema_version=2,
            package_id=package_id,
            source_id=source_id,
            revision=1,
            title=response.title.strip() or _topic(response.body),
            source_text=source_text,
            source_type=source_type,
            draft_body=body,
            questions=[_with_excerpt(q, source_text) for q in response.questions[:3]],
            stage="검토",
            stage_times={"정리": timestamp, "질문": timestamp, "검토": timestamp},
        )
        if not response.script.strip():
            raise ValueError("revised script must not be empty")
        with self.state.lock():
            self.state.save_package(package, response.script)
        self.proposals.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.proposals / f"{package_id}.r1.json", package)
        return package, response.script


def _topic(body: str) -> str:
    lines = [re.sub(r"^[#*>\s-]+", "", line).strip() for line in body.splitlines()]
    useful = [
        line
        for line in lines
        if line and line not in {"요약", "통화 요약", "대화 요약"}
    ]
    return (useful or [line for line in lines if line] or ["검토할 내용"])[0][:100]


def _with_excerpt(question: Question, source: str) -> Question:
    if question.source_excerpt.strip() and question.source_excerpt in source:
        return question
    # Ground fallback excerpts in the original source, never in revised model text.
    chunks = [
        match
        for match in re.finditer(r"[^\n.!?]+[.!?]?", source)
        if match.group().strip()
    ]
    tokens = set(
        re.findall(
            r"[\w가-힣]{2,}",
            " ".join([question.question, question.rationale, *question.choices]),
        )
    )
    if chunks:
        best = max(
            chunks, key=lambda match: sum(token in match.group() for token in tokens)
        )
        excerpt = source[
            max(0, best.start() - 120) : min(len(source), best.end() + 120)
        ]
    else:
        excerpt = source
    return question.model_copy(update={"source_excerpt": excerpt[:600]})
