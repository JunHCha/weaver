"""Synthetic approval rule evaluation, with replay or an explicitly supplied client."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from weaver.context import ContextDictionary, ContextEntry
from weaver.llm.types import LlmClient
from weaver.loom.reextract import Bootstrap, MemoryDictionary, _bootstrap
from weaver.loom.summary_review import ApprovalExtractor
from weaver.schema import Question, ReviewPackage, Thread, ThreadDraft, Verdict

from .client import TracedClient
from .summary_runner import _Sink

APPROVAL_CASES = Path(__file__).resolve().parents[3] / "evals" / "approval_cases.json"
NOW = datetime(2032, 1, 1, tzinfo=UTC)
SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def evaluate_approval(case: dict[str, Any], client: LlmClient) -> dict[str, Any]:
    traced = TracedClient(client)
    with TemporaryDirectory(prefix="weaver-approval-eval-") as directory:
        context = ContextDictionary(Path(directory))
        for entry in case["dictionary"]:
            context.add_entry(
                entry["name"], entry["kind"], tuple(entry.get("aliases", []))
            )
        question = case.get("question")
        package = ReviewPackage(
            schema_version=2,
            package_id="eval",
            source_id=SOURCE,
            revision=1,
            draft_body=case["body"],
            stage="검토",
            stage_times={"검토": NOW},
            questions=[
                Question(
                    question=question,
                    rationale="합성 검토",
                    choices=["김철수", "김영희", "모름"],
                    source_excerpt=case["script"],
                )
            ]
            if question
            else [],
        )
        verdict = Verdict(
            schema_version=2,
            package_id="eval",
            revision=1,
            action="approve",
            answers=case["answers"],
            edited_body=None,
            feedback=None,
        )
        prepared = ApprovalExtractor(
            traced,
            context,
            _Sink(),
            contact_name=case.get("contact_name"),
            source_occurred_at=datetime.fromisoformat(case["source_occurred_at"])
            if case.get("source_occurred_at")
            else NOW,
        ).prepare(package, case["script"], verdict, now=NOW)
        expected = case["expected"]
        tags = {tag for thread in prepared.threads for tag in thread.tags}
        aliases = {alias for entry in prepared.new_entries for alias in entry.aliases}
        return {
            "case_id": case["case_id"],
            "metrics": {
                "facts.count": len(prepared.threads) == expected["count"],
                "parties.tags": tags == set(expected["tags"]),
                "review_meta.absent": not any(
                    word in (thread.content or "")
                    for word in expected["forbidden_content"]
                    for thread in prepared.threads
                ),
                "misrecognition_alias.absent": not aliases.intersection(
                    expected["forbidden_aliases"]
                ),
                "corrections.exact": [c.model_dump() for c in prepared.corrections]
                == expected["corrections"],
                "titles.absent": not any(
                    word in tag for word in ("팀장", "수석", "지원팀") for tag in tags
                ),
            },
            "prediction": prepared.model_dump(mode="json"),
            "calls": traced.calls,
        }


def evaluate_bootstrap(case: dict[str, Any], client: LlmClient) -> dict[str, Any]:
    traced = TracedClient(client)
    memory = MemoryDictionary([ContextEntry(**entry) for entry in case["dictionary"]])
    changes = Bootstrap()
    errors: list[str] = []
    with TemporaryDirectory(prefix="weaver-bootstrap-eval-") as directory:
        thread = Thread(
            **ThreadDraft(
                thread_id="synthetic",
                kind="기록",
                content="합성",
                occurred_at=NOW,
                recorded_at=NOW,
                tags=case["tags"],
                source_id=SOURCE,
                actor="human",
                field=None,
                value=None,
                direction=None,
                resolves=None,
                retracts=None,
                correction=None,
            ).model_dump(),
            seq=1,
        )
        additions = _bootstrap(
            traced, memory, [thread], Path(directory), errors, changes
        )
    entry = memory.snapshot().resolve(case["expected_name"])
    return {
        "case_id": "bootstrap-clean-title",
        "metrics": {
            "name.cleaned": bool(entry and entry.name == case["expected_name"]),
            "old_spelling.not_alias": bool(
                entry and not set(entry.aliases).intersection(case["tags"])
            ),
            "rename.proposed": any(
                c.canonical == case["expected_name"] for c in changes.renames
            ),
            "tag_mapping.proposed": all(
                any(
                    c.observed == tag and c.canonical == case["expected_name"]
                    for c in changes.tag_corrections
                )
                for tag in case["tags"]
            ),
            "errors.absent": not errors,
        },
        "prediction": {
            "changes": changes.model_dump(),
            "additions": [e.model_dump() for e in additions],
        },
        "calls": traced.calls,
    }
