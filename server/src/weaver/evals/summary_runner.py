"""Evaluate document drafting and approved Thread extraction in isolation."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from weaver.context import ContextDictionary
from weaver.llm.types import LlmClient
from weaver.loom.summary import SummaryDrafts
from weaver.loom.summary_review import SummaryReview
from weaver.schema import Thread, ThreadDraft, Verdict

from .client import TracedClient

SUMMARY_CASES = Path(__file__).resolve().parents[3] / "evals" / "summary_cases.json"


class _Sink:
    def __init__(self) -> None:
        self.drafts: list[ThreadDraft] = []

    def append(self, threads: list[ThreadDraft]) -> list[Thread]:
        self.drafts.extend(threads)
        return [
            Thread(**draft.model_dump(), seq=i + 1) for i, draft in enumerate(threads)
        ]


def evaluate_summary(case: dict[str, Any], client: LlmClient) -> dict[str, Any]:
    """Run the production draft and approve stages against one independent case."""
    traced = TracedClient(client)
    with TemporaryDirectory(prefix="weaver-summary-eval-") as directory:
        root = Path(directory)
        context = ContextDictionary(root / "state")
        package, script = SummaryDrafts(
            traced, context, root / "proposals", state_root=root / "state"
        ).create(case["source_id"], case["script"], package_id="eval")
        sink = _Sink()
        verdict = Verdict(
            schema_version=2,
            package_id="eval",
            revision=1,
            action="approve",
            answers=case["answers"],
            edited_body=None,
            feedback=None,
        )
        final = SummaryReview(
            traced, context, sink, root / "archive", state_root=root / "state"
        ).approve(package, script, verdict)
        threads = [
            {key: getattr(thread, key) for key in ("kind", "content", "tags")}
            for thread in sink.drafts
        ]
        expected = case["expected"]
        return {
            "case_id": case["case_id"],
            "metrics": {
                "draft.exact": package.draft_body == expected["body"],
                "questions.exact": [q.model_dump() for q in package.questions]
                == expected["questions"],
                "threads.exact": threads == expected["threads"],
            },
            "prediction": {
                "body": package.draft_body,
                "questions": [q.model_dump() for q in package.questions],
                "final_body": final.body,
                "threads": threads,
            },
            "calls": traced.calls,
        }
