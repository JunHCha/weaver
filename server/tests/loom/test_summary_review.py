from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from weaver.context import ContextDictionary
from weaver.loom.summary_review import SummaryReview
from weaver.schema import ReviewPackage, Thread, ThreadDraft, Verdict

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


def test_edit_feedback_reject_and_replay(tmp_path: Path) -> None:
    class Llm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            if task == "loom.feedback":
                return schema.model_validate(
                    {"patches": [{"before": "결정했다", "after": "논의했다"}]}
                )
            data = super().generate_json(task, system, user, schema).model_dump()
            data["corrections"] = []
            return schema.model_validate(data)

    def make() -> SummaryReview:
        return SummaryReview(
            Llm(),
            ContextDictionary(tmp_path / "state"),
            sink,
            tmp_path / "archive",
            state_root=tmp_path / "state",
            proposals=tmp_path / "proposals",
        )

    sink = FakeSink()
    base = dict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        answers=[],
        edited_body=None,
        feedback=None,
    )
    feedback = Verdict.model_validate(
        base | {"action": "feedback", "feedback": "표현 수정"}
    )
    revised = make().apply(package(), "원문", feedback, now=NOW)
    assert isinstance(revised, ReviewPackage)
    assert revised.revision == 2 and revised.draft_body == "홍길돈이 논의했다."
    assert make().apply(package(), "원문", feedback, now=NOW) == revised
    assert "-홍길돈이 결정했다." in (tmp_path / "proposals/pkg.r2.diff").read_text()
    edit = Verdict.model_validate(
        base | {"revision": 2, "action": "edit", "edited_body": "사람이 수정한 본문"}
    )
    summary = make().apply(revised, "원문", edit, now=NOW)
    assert summary.body == "사람이 수정한 본문"
    assert sink.drafts[0].actor == "human"
    published = ReviewPackage.model_validate_json(
        (tmp_path / "proposals/pkg.r2.json").read_text()
    )
    assert published.stage == "반영" and "반영" in published.stage_times
    other = package().model_copy(update={"package_id": "other"})
    rejection = Verdict.model_validate(
        base | {"package_id": "other", "action": "reject"}
    )
    make().apply(other, "원문", rejection, now=NOW)
    make().apply(other, "원문", rejection, now=NOW)
    assert len(sink.drafts) == 2 and sink.drafts[-1].kind == "거절"


def test_fabric_lost_response_replays_same_batch(tmp_path: Path) -> None:
    import pytest

    from weaver.fabric import FabricStore, ThreadQuery

    store = FabricStore(tmp_path / "fabric.sqlite3")

    class Lost:
        def append(self, drafts: list[ThreadDraft]) -> list[Thread]:
            store.append(drafts)
            raise RuntimeError("lost response")

    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["홍길동"],
        edited_body=None,
        feedback=None,
    )
    context = ContextDictionary(tmp_path / "state")
    review = SummaryReview(
        FakeLlm(), context, Lost(), tmp_path / "archive", state_root=tmp_path / "state"
    )
    with pytest.raises(RuntimeError):
        review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    review = SummaryReview(
        FakeLlm(), context, store, tmp_path / "archive", state_root=tmp_path / "state"
    )
    summary = review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    rows = store.entries(ThreadQuery())
    assert len(rows) == 1
    assert (
        rows[0].thread_id
        == "thr_"
        + __import__("hashlib").sha256(f"{summary.summary_id}:0".encode()).hexdigest()
    )


def test_feedback_publication_failure_replays_prepared_revision(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest

    import weaver.loom.summary_review as module

    class FeedbackLlm(FakeLlm):
        calls = 0

        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            self.calls += 1
            return schema.model_validate(
                {"patches": [{"before": "결정했다", "after": "논의했다"}]}
            )

    llm = FeedbackLlm()
    sink = FakeSink()
    review = SummaryReview(
        llm,
        ContextDictionary(tmp_path / "state"),
        sink,
        tmp_path / "archive",
        state_root=tmp_path / "state",
        proposals=tmp_path / "proposals",
    )
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="feedback",
        answers=[],
        edited_body=None,
        feedback="표현 수정",
    )
    original = module.write_json_atomic

    def fail(*args, **kwargs):
        raise OSError("publication interrupted")

    monkeypatch.setattr(module, "write_json_atomic", fail)
    with pytest.raises(OSError):
        review.apply(package(), "원문", verdict, now=NOW)
    monkeypatch.setattr(module, "write_json_atomic", original)
    result = review.apply(package(), "원문", verdict, now=NOW)
    assert isinstance(result, ReviewPackage) and result.revision == 2
    assert llm.calls == 1
    assert review.state.load_package("pkg", 2) == (result, "원문")
    assert (tmp_path / "proposals/pkg.r2.diff").is_file()


def test_schedule_extraction_links_only_explicit_request_and_uses_timezone(
    tmp_path: Path,
) -> None:
    import json

    class Llm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            assert "명시" in system and "blocked_by_index" in system
            for rule in (
                "배열 범위",
                "start보다 늦어야",
                "IANA",
                "원문에 실제로",
                "정확히 일치",
                "존재하지 않는",
                "{add:",
            ):
                assert rule in system
            assert json.loads(user)["timezone"] == "America/New_York"
            base = {
                "occurred_at": NOW.isoformat(),
                "tags": [],
                "direction": None,
                "resolves": None,
                "retracts": None,
            }
            return schema.model_validate(
                {
                    "body": "요청을 마치면 회의한다. 별도 회의도 한다.",
                    "corrections": [],
                    "threads": [
                        base
                        | {
                            "kind": "요청",
                            "content": "자료를 요청했다",
                            "direction": "받음",
                            "field": None,
                            "value": None,
                        },
                        base
                        | {
                            "kind": "일정",
                            "content": "요청을 마치면 회의한다",
                            "field": "schedule",
                            "value": {
                                "start": "2026-01-02T09:00:00",
                                "end": "2026-01-02T10:00:00",
                                "timezone": None,
                            },
                            "blocked_by_index": 0,
                        },
                        base
                        | {
                            "kind": "일정",
                            "content": "별도 회의",
                            "field": "schedule",
                            "value": {"start": "2026-01-03T09:00:00", "end": None},
                            "blocked_by_index": None,
                        },
                    ],
                }
            )

    sink = FakeSink()
    review = SummaryReview(
        Llm(),
        ContextDictionary(tmp_path / "state"),
        sink,
        tmp_path / "archive",
        state_root=tmp_path / "state",
        timezone="America/New_York",
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
    review.approve(package(), "원문", verdict, now=NOW)
    assert sink.drafts[1].blocked_by == sink.drafts[0].thread_id
    assert sink.drafts[2].blocked_by is None
    assert sink.drafts[1].value == {
        "start": "2026-01-02T09:00:00-05:00",
        "end": "2026-01-02T10:00:00-05:00",
        "timezone": "America/New_York",
    }


def test_schedule_extraction_rejects_invalid_times_and_references(
    tmp_path: Path,
) -> None:
    import pytest

    class Llm(FakeLlm):
        value: object = None
        index: int | None = None

        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            return schema.model_validate(
                {
                    "body": "회의",
                    "corrections": [],
                    "threads": [
                        {
                            "kind": "일정",
                            "content": "회의",
                            "occurred_at": NOW.isoformat(),
                            "tags": [],
                            "field": "schedule",
                            "value": self.value,
                            "direction": None,
                            "resolves": None,
                            "retracts": None,
                            "blocked_by_index": self.index,
                        }
                    ],
                }
            )

    llm = Llm()
    sink = FakeSink()
    review = SummaryReview(
        llm,
        ContextDictionary(tmp_path / "state"),
        sink,
        tmp_path / "archive",
        state_root=tmp_path / "state",
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
    for value, index in [
        ({"start": "invalid", "end": None}, None),
        ({"start": "2026-01-02T09:00:00", "timezone": "Invalid/Zone"}, None),
        ({"start": "2026-03-08T02:30:00", "timezone": "America/New_York"}, None),
        ({"start": "2026-01-02T10:00:00", "end": "2026-01-02T09:00:00"}, None),
        ({"start": "2026-01-02T10:00:00", "end": None}, 0),
    ]:
        llm.value, llm.index = value, index
        with pytest.raises(ValueError) as caught:
            review.approve(package(), "원문", verdict, now=NOW)
        from weaver.failures import failure_reason

        assert "operation_failed" not in failure_reason(caught.value)
    assert sink.drafts == []


def test_freeform_answers_and_feedback_use_one_approval_request(tmp_path: Path) -> None:
    import json

    class RecordingLlm(FakeLlm):
        calls: list[tuple[str, str]] = []

        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            self.calls.append((task, user))
            return schema.model_validate(
                {"body": "추가 답변 반영", "corrections": [], "threads": []}
            )

    llm = RecordingLlm()
    review = SummaryReview(
        llm,
        ContextDictionary(tmp_path / "state"),
        FakeSink(),
        tmp_path / "archive",
        state_root=tmp_path / "state",
    )
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["보기 밖의 자유 답변", "두 번째 답", "세 번째 답"],
        feedback="추가 요청도 반영",
        edited_body=None,
    )
    batch = package().model_copy(update={"questions": package().questions * 3})
    result = review.approve(batch, "원문", verdict, now=NOW)
    assert result.body == "추가 답변 반영"
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == "loom.approve"
    payload = json.loads(llm.calls[0][1])
    assert payload["answers"] == verdict.answers
    assert payload["feedback"] == verdict.feedback
    assert len(payload["questions"]) == 3


def test_feedback_request_includes_collected_answers(tmp_path: Path) -> None:
    import json

    class RecordingLlm(FakeLlm):
        calls: list[str] = []

        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            assert task == "loom.feedback"
            self.calls.append(user)
            return schema.model_validate({"patches": []})

    llm = RecordingLlm()
    review = SummaryReview(
        llm,
        ContextDictionary(tmp_path / "state"),
        FakeSink(),
        tmp_path / "archive",
        state_root=tmp_path / "state",
    )
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="feedback",
        answers=["직접 입력한 이름"],
        feedback="수정",
        edited_body="직접 수정한 본문",
    )
    result = review.apply(package(), "원문", verdict, now=NOW)
    assert isinstance(result, ReviewPackage)
    assert result.draft_body == "직접 수정한 본문"
    assert result.revision == 2
    assert json.loads(llm.calls[0])["body"] == "직접 수정한 본문"
    assert len(llm.calls) == 1
    assert json.loads(llm.calls[0])["answers"] == verdict.answers


def test_edit_preserves_human_body_with_collected_answers(tmp_path: Path) -> None:
    class ExtractionLlm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            return schema.model_validate(
                {"body": "모델이 바꾼 문장", "corrections": [], "threads": []}
            )

    review = SummaryReview(
        ExtractionLlm(),
        ContextDictionary(tmp_path / "state"),
        FakeSink(),
        tmp_path / "archive",
        state_root=tmp_path / "state",
    )
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="edit",
        answers=["자유 답변"],
        edited_body="사람이 직접 쓴 문장",
        feedback=None,
    )
    result = review.apply(package(), "원문", verdict, now=NOW)
    assert result is not None
    assert result.body == verdict.edited_body


def test_unverified_corrections_are_dropped_not_fatal(tmp_path: Path) -> None:
    class Llm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            data = super().generate_json(task, system, user, schema).model_dump()
            data["corrections"] = [
                {"observed": "홍길돈", "canonical": "홍길동", "kind": "person"},
                {"observed": "홍길돈", "canonical": "직접 입력한 답", "kind": "person"},
                {"observed": "원문에 없음", "canonical": "홍길동", "kind": "person"},
            ]
            return schema.model_validate(data)

    sink = FakeSink()
    context = ContextDictionary(tmp_path / "state")
    review = SummaryReview(
        Llm(), context, sink, tmp_path / "archive", state_root=tmp_path / "state"
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
    assert len(sink.drafts) == 1
    assert [c.name for c in context.candidates("홍길돈")] == ["홍길동"]
    assert context.candidates("원문에 없음") == []
