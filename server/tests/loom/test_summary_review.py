from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from weaver.context import ContextDictionary
from weaver.loom.summary_review import SummaryReview
from weaver.schema.v2 import ReviewPackage, Thread, ThreadDraft, Verdict

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"


class FakeLlm:
    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        return schema.model_validate(
            {
                "body": "홍길동이 결정했다.",
                "corrections": [
                    {"observed": "홍길돈", "canonical": "홍길동", "kind": "person"}
                ],
                "threads": [
                    {
                        "kind": "결정",
                        "content": "홍길동이 결정했다.",
                        "occurred_at": NOW.isoformat(),
                        "tags": ["홍길동"],
                        "field": None,
                        "value": None,
                        "direction": None,
                        "resolves": None,
                        "retracts": None,
                    }
                ],
            }
        )

    def generate_text(self, task: str, system: str, user: str) -> str:
        raise AssertionError("unexpected call")


class FakeSink:
    def __init__(self) -> None:
        self.drafts: list[ThreadDraft] = []

    def append(self, threads: list[ThreadDraft]) -> list[Thread]:
        self.drafts.extend(threads)
        return [
            Thread(**draft.model_dump(), seq=i + 1) for i, draft in enumerate(threads)
        ]


def package() -> ReviewPackage:
    return ReviewPackage(
        schema_version=2,
        package_id="pkg",
        source_id=SOURCE,
        revision=1,
        draft_body="홍길돈이 결정했다.",
        questions=[
            {
                "question": "누구인가?",
                "rationale": "이름 불확실",
                "choices": ["홍길동", "김철수", "모름"],
            }
        ],
        stage="검토",
        stage_times={"검토": NOW},
    )


def test_approve_archives_extracts_and_learns(tmp_path: Path) -> None:
    sink = FakeSink()
    context = ContextDictionary(tmp_path / "state")
    review = SummaryReview(
        FakeLlm(), context, sink, tmp_path / "archive", state_root=tmp_path / "state"
    )
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["홍길동"],
        edited_body=None,
        feedback=None,
    )
    summary = review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    assert summary.body == "홍길동이 결정했다."
    assert sink.drafts[0].source_id == SOURCE
    assert sink.drafts[0].actor == "ai_approved"
    assert context.candidates("홍길돈")[0].name == "홍길동"
    assert (
        tmp_path / "archive" / summary.summary_id[:2] / f"{summary.summary_id}.json"
    ).is_file()


def test_approve_restarts_without_duplicate_or_new_llm_call(tmp_path: Path) -> None:
    sink = FakeSink()
    context = ContextDictionary(tmp_path / "state")
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["홍길동"],
        edited_body=None,
        feedback=None,
    )
    review = SummaryReview(
        FakeLlm(), context, sink, tmp_path / "archive", state_root=tmp_path / "state"
    )
    first = review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    second = SummaryReview(
        FakeLlm(), context, sink, tmp_path / "archive", state_root=tmp_path / "state"
    )
    assert second.approve(package(), "홍길돈 원문", verdict, now=NOW) == first
    assert len(sink.drafts) == 1
    assert second.state.load_package("pkg", 1) == (package(), "홍길돈 원문")


def test_sink_failure_replays_prepared_threads(tmp_path: Path) -> None:
    class RetrySink(FakeSink):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def append(self, threads: list[ThreadDraft]) -> list[Thread]:
            if not self.failed:
                self.failed = True
                self.drafts.extend(threads)
                raise RuntimeError("lost response")
            assert self.drafts == threads
            return [
                Thread(**draft.model_dump(), seq=i + 1)
                for i, draft in enumerate(threads)
            ]

    import pytest

    sink = RetrySink()
    context = ContextDictionary(tmp_path / "state")
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["홍길동"],
        edited_body=None,
        feedback=None,
    )
    review = SummaryReview(
        FakeLlm(), context, sink, tmp_path / "archive", state_root=tmp_path / "state"
    )
    with pytest.raises(RuntimeError):
        review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    review = SummaryReview(
        FakeLlm(), context, sink, tmp_path / "archive", state_root=tmp_path / "state"
    )
    review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    assert len(sink.drafts) == 1


def test_conflicting_verdict_and_package_are_rejected(tmp_path: Path) -> None:
    import pytest

    sink = FakeSink()
    context = ContextDictionary(tmp_path / "state")
    review = SummaryReview(
        FakeLlm(), context, sink, tmp_path / "archive", state_root=tmp_path / "state"
    )
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["홍길동"],
        edited_body=None,
        feedback=None,
    )
    review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    with pytest.raises(ValueError, match="conflicting verdict"):
        review.approve(
            package(),
            "홍길돈 원문",
            verdict.model_copy(update={"answers": ["김철수"]}),
            now=NOW,
        )
    with pytest.raises(ValueError, match="conflicting package"):
        review.approve(
            package().model_copy(update={"draft_body": "다른 문서"}),
            "홍길돈 원문",
            verdict,
            now=NOW,
        )
    assert len(sink.drafts) == 1
