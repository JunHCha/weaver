"""Approving a review turns the reviewed script into Threads, exactly once.

The approval prompt carries only a budgeted slice of the dictionary and
existing tags (areas and transcript matches first, then recent tags), yet every
returned tag is validated against the full dictionary. Human edits, answers and
feedback are preserved; a failed sink, lost response or crash replays the same
prepared batch instead of calling the model again. Synthetic reviews and a fake
model only."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel
from server.tests.fake_responses import fixture_response

from weaver.context import ContextDictionary
from weaver.loom.summary_review import SummaryReview
from weaver.schema import ReviewPackage, Thread, ThreadDraft, Verdict

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"


class FakeLlm:
    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        return fixture_response(
            schema,
            {
                "new_entries": [{"name": "홍길동", "kind": "person"}],
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
            },
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


def test_edit_reject_and_replay(tmp_path: Path) -> None:
    class Llm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            data = super().generate_json(task, system, user, schema).model_dump()
            data["corrections"] = []
            return fixture_response(schema, data)

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
    edit = Verdict.model_validate(
        base | {"action": "edit", "edited_body": "사람이 수정한 본문"}
    )
    summary = make().apply(package(), "원문", edit, now=NOW)
    assert summary.body == "사람이 수정한 본문"
    assert sink.drafts[0].actor == "human"
    published = ReviewPackage.model_validate_json(
        (tmp_path / "proposals/pkg.r1.json").read_text()
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
            return fixture_response(
                schema,
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
                },
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
            return fixture_response(
                schema,
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
                },
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
            return fixture_response(
                schema, {"body": "추가 답변 반영", "corrections": [], "threads": []}
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


def test_edit_preserves_human_body_with_collected_answers(tmp_path: Path) -> None:
    class ExtractionLlm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            return fixture_response(
                schema, {"body": "모델이 바꾼 문장", "corrections": [], "threads": []}
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
            return fixture_response(schema, data)

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


@pytest.mark.parametrize("projection", ["enabled", "disabled", "io_error"])
def test_prepared_legacy_feedback_is_recovered_without_llm(
    tmp_path: Path, monkeypatch, projection
) -> None:
    import json

    from weaver.runtime.summary import ReviewAdapter

    class NoLlm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            raise AssertionError("recovery must not call the LLM")

    review = SummaryReview(
        NoLlm(),
        ContextDictionary(tmp_path / "state"),
        FakeSink(),
        tmp_path / "archive",
        state_root=tmp_path / "state",
        publish_files=projection != "disabled",
    )
    old = package()
    revised = old.model_copy(update={"revision": 2, "draft_body": "수정된 본문"})
    review.state.save_package(old, "원문")
    verdict = {
        "schema_version": 2,
        "package_id": "pkg",
        "revision": 1,
        "action": "feedback",
        "answers": [],
        "edited_body": None,
        "feedback": "수정",
    }
    prepared = {
        "package": revised.model_dump(mode="json"),
        "diff": "diff",
        "applied_at": NOW.isoformat(),
    }
    with review.state.connect() as db:
        db.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, 'PREPARED')",
            ("pkg", 1, json.dumps(verdict), json.dumps(prepared)),
        )
    if projection == "io_error":

        def fail(*args):
            raise OSError("synthetic projection failure")

        monkeypatch.setattr("weaver.loom.projection.write_bytes_atomic", fail)
        monkeypatch.setattr("weaver.loom.projection.write_json_atomic", fail)
    ReviewAdapter(review, None)  # type: ignore[arg-type]
    assert review.state.load_package("pkg", 2) == (revised, "원문")
    if projection == "enabled":
        assert (review.proposals / "pkg.r2.diff").read_text() == "diff"
    else:
        assert not (review.proposals / "pkg.r2.diff").exists()
    # HTTP's full revision body is independent of the optional legacy diff.
    from weaver.mobile.reviews import ReviewService
    from weaver.mobile.store import MobileStore

    service = ReviewService(review.state, MobileStore(tmp_path / "state"))
    assert service.package("pkg", 2) == revised
    monkeypatch.undo()
    service.republish(review.proposals)
    assert (review.proposals / "pkg.r2.diff").read_text() == "diff"
    assert (
        ReviewPackage.model_validate_json(
            (review.proposals / "pkg.r2.json").read_bytes()
        )
        == revised
    )
    with review.state.connect() as db:
        assert db.execute("SELECT stage FROM reviews").fetchone()[0] == "APPLIED"


def test_schedule_change_links_only_a_real_existing_schedule(tmp_path: Path) -> None:
    import json

    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.schema import ThreadDraft

    value = {
        "start": "2026-01-02T09:00:00+09:00",
        "end": None,
        "timezone": "Asia/Seoul",
    }
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    seeded = [
        ThreadDraft(
            thread_id=key,
            kind=kind,
            content=key,
            occurred_at=NOW,
            recorded_at=NOW,
            tags=["홍길동"],
            field="schedule" if kind == "일정" else None,
            value=value if kind == "일정" else None,
            source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            actor="ai_approved",
            direction=None,
            resolves=None,
            retracts=None,
            correction=None,
        )
        for key, kind in (("old", "일정"), ("note", "기록"))
    ]
    fabric.append(seeded)

    class Llm(FakeLlm):
        link: str | None = None
        kind = "일정"
        seen: dict[str, object] = {}
        calls_made: list[int] = []

        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            assert "existing_schedules" in system and "updates" in system
            self.seen = json.loads(user)
            self.calls_made.append(1)
            self.calls_made[:] = self.calls_made
            plan = self.kind == "일정"
            return fixture_response(
                schema,
                {
                    "body": f"회의를 옮긴다 {len(self.calls_made)}",
                    "corrections": [],
                    "threads": [
                        {
                            "kind": self.kind,
                            "content": "회의",
                            "occurred_at": NOW.isoformat(),
                            "tags": ["홍길동"],
                            "field": "schedule" if plan else None,
                            "value": {
                                "start": "2026-01-03T09:00:00+09:00",
                                "end": None,
                                "timezone": "Asia/Seoul",
                            }
                            if plan
                            else None,
                            "direction": None,
                            "resolves": None,
                            "retracts": None,
                            "blocked_by_index": None,
                            "updates": self.link,
                        }
                    ],
                },
            )

    llm = Llm()
    review = SummaryReview(
        llm,
        ContextDictionary(tmp_path / "state"),
        fabric,
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
    for n, (link, kind, expected) in enumerate(
        [
            ("old", "일정", "old"),
            ("missing", "일정", None),
            ("note", "일정", None),
            ("old", "기록", None),
            (None, "일정", None),
        ]
    ):
        llm.link, llm.kind = link, kind
        pkg = package().model_copy(update={"package_id": f"pkg{n}"})
        review.approve(
            pkg, "원문", verdict.model_copy(update={"package_id": f"pkg{n}"}), now=NOW
        )
        assert (
            max(fabric.entries(ThreadQuery()), key=lambda t: t.seq).updates == expected
        )
    assert [s["id"] for s in llm.seen["existing_schedules"]][:1] == ["old"]


def test_dictionary_tags_prompt_validation_and_retry(tmp_path: Path, caplog) -> None:
    import json
    import unicodedata

    import pytest

    from weaver.fabric import FabricStore, ThreadQuery

    context = ContextDictionary(tmp_path / "state")
    context.add_entry("표준", "project", ("Alias",))
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append(
        [
            ThreadDraft(
                thread_id="existing",
                kind="기록",
                content="기존",
                occurred_at=NOW,
                recorded_at=NOW,
                tags=["기존태그"],
                field=None,
                value=None,
                source_id=SOURCE,
                actor="human",
                direction=None,
                resolves=None,
                retracts=None,
                correction=None,
            )
        ]
    )

    class DictionaryLlm(FakeLlm):
        calls = 0

        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            self.calls += 1
            prompt = json.loads(user)
            assert prompt["dictionary"] == [
                {"name": "표준", "kind": "project", "aliases": ["Alias"]}
            ]
            assert prompt["existing_tags"] == ["기존태그"]
            assert "new_entries" in system
            data = super().generate_json(task, system, user, schema).model_dump()
            data["new_entries"] = [
                {
                    "name": unicodedata.normalize("NFD", "새사람"),
                    "kind": "person",
                    "aliases": ["NewAlias"],
                },
                {"name": "충돌", "kind": "topic", "aliases": ["ALIAS"]},
            ]
            data["corrections"] = []
            data["threads"][0]["tags"] = [
                "ALIAS",
                "표준",
                "newalias",
                "비공개미확인태그",
            ]
            return fixture_response(schema, data)

    class InterruptedSink:
        failed = False

        def entries(self, query):
            return fabric.entries(query)

        def append(self, drafts):
            assert context.resolve("새사람") is not None
            result = fabric.append(drafts)
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic lost response")
            return result

    llm, sink = DictionaryLlm(), InterruptedSink()
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
        llm, context, sink, tmp_path / "archive", state_root=tmp_path / "state"
    )
    with pytest.raises(RuntimeError):
        review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    context.set_kind("새사람", "organization")
    review = SummaryReview(
        llm, context, sink, tmp_path / "archive", state_root=tmp_path / "state"
    )
    review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    assert llm.calls == 1
    assert fabric.entries(ThreadQuery())[-1].tags == ["표준", "새사람", "업무 운영"]
    assert len(context.list_entries()) == 3
    assert context.resolve("NewAlias").human_edited
    assert context.kind_of("새사람") == "organization"
    assert "dropped unresolved approval tag" in caplog.text
    assert "비공개미확인태그" not in caplog.text


def test_apply_racing_alias_collision_never_wedges_append(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    import sqlite3

    context = ContextDictionary(tmp_path / "state")
    add = context.add_entry
    calls = []

    def racing_add(name, kind, aliases=(), **kwargs):
        with sqlite3.connect(tmp_path / "state" / "summary.sqlite3") as db:
            assert db.execute("SELECT stage FROM reviews").fetchone() == ("PREPARED",)
        if not calls:
            add("개인항목", "person", ("경쟁별칭",))
        calls.append(name)
        return add(name, kind, aliases, **kwargs)

    class Llm(FakeLlm):
        calls = 0

        def generate_json(self, task, system, user, schema):
            self.calls += 1
            data = super().generate_json(task, system, user, schema).model_dump()
            data["new_entries"] = [
                {
                    "name": "새항목",
                    "kind": "topic",
                    "aliases": ["경쟁별칭", "안전별칭"],
                }
            ]
            data["corrections"] = []
            data["threads"][0]["tags"] = ["경쟁별칭"]
            return fixture_response(schema, data)

    monkeypatch.setattr(context, "add_entry", racing_add)
    sink, llm = FakeSink(), Llm()
    review = SummaryReview(
        llm, context, sink, tmp_path / "archive", state_root=tmp_path / "state"
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
    result = review.approve(package(), "원문", verdict, now=NOW)
    assert review.approve(package(), "원문", verdict, now=NOW) == result
    assert len(sink.drafts) == 1 and llm.calls == 1
    assert sink.drafts[0].tags == ["새항목", "업무 운영"]
    assert context.resolve("안전별칭").name == "새항목"
    assert context.resolve("경쟁별칭").name == "개인항목"
    assert "dropped conflicting dictionary aliases" in caplog.text
    assert "경쟁별칭" not in caplog.text


def test_prepare_keeps_safe_aliases_and_only_used_proposals(
    tmp_path: Path, monkeypatch
) -> None:
    context = ContextDictionary(tmp_path / "state")
    context.add_entry("기존", "person", ("충돌",))
    snapshot = context.snapshot
    reads = []

    def counted_snapshot():
        reads.append(1)
        return snapshot()

    class Llm(FakeLlm):
        def generate_json(self, task, system, user, schema):
            data = super().generate_json(task, system, user, schema).model_dump()
            data["new_entries"] = [
                {"name": "사용", "kind": "topic", "aliases": ["충돌", "안전"]},
                {"name": "미사용", "kind": "topic", "aliases": []},
            ]
            data["threads"][0]["tags"] = ["안전", "충돌", "사용"]
            data["corrections"] = []
            return fixture_response(schema, data)

    monkeypatch.setattr(context, "snapshot", counted_snapshot)
    sink = FakeSink()
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
    review.approve(package(), "원문", verdict, now=NOW)
    assert len(reads) == 1
    assert sink.drafts[0].tags == ["사용", "기존", "업무 운영"]
    assert snapshot().resolve("사용").aliases == ("안전",)
    assert snapshot().resolve("미사용") is None


def test_large_dictionary_and_tags_obey_token_budget_and_priority(
    tmp_path: Path,
) -> None:
    import json

    from weaver.context import Page
    from weaver.loom.summary_review import TAG_CONTEXT_TOKEN_BUDGET

    # Both sets only need to exceed the prompt budget, so the sizes stay small.
    dictionary_size, tag_count = 120, 150
    last_entry = f"A{dictionary_size - 1:03d}"
    context = ContextDictionary(tmp_path / "state")
    context.sync_from_pages(
        [
            *(
                Page(f"A{i:03d}", "topic", (f"별칭{i}-{'y' * 60}",))
                for i in range(dictionary_size)
            ),
            Page("ZrecentUnique", "person"),
            Page("ZtranscriptUnique", "topic"),
        ]
    )
    thread = ThreadDraft(
        thread_id="old",
        kind="기록",
        content="기존",
        occurred_at=NOW,
        recorded_at=NOW,
        tags=["A000"],
        field=None,
        value=None,
        source_id=SOURCE,
        actor="human",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )

    class Sink(FakeSink):
        def entries(self, query):
            return [
                Thread(**thread.model_dump(), seq=1),
                Thread(
                    **(
                        thread.model_dump()
                        | {
                            "thread_id": "recent",
                            "tags": [
                                "ZrecentUnique",
                                *(
                                    f"huge-tag-{i:04d}-{'x' * 80}"
                                    for i in range(tag_count)
                                ),
                            ],
                        }
                    ),
                    seq=2,
                ),
            ]

    class Llm(FakeLlm):
        def generate_json(self, task, system, user, schema):
            prompt = json.loads(user)
            hints = {key: prompt[key] for key in ("dictionary", "existing_tags")}
            assert (
                len(json.dumps(hints, ensure_ascii=False).encode("utf-8"))
                <= TAG_CONTEXT_TOKEN_BUDGET
            )
            names = [e["name"] for e in prompt["dictionary"]]
            assert names[:2] == ["ZtranscriptUnique", "ZrecentUnique"]
            assert last_entry not in names
            assert prompt["existing_tags"][0] == "ZrecentUnique"
            assert not any(
                tag.startswith(f"huge-tag-{tag_count - 1:04d}")
                for tag in prompt["existing_tags"]
            )
            data = super().generate_json(task, system, user, schema).model_dump()
            data["threads"][0]["tags"] = [last_entry]
            data["corrections"] = []
            return fixture_response(schema, data)

    sink = Sink()
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
    review.approve(package(), "ZtranscriptUnique", verdict, now=NOW)
    # Tags outside the prompt budget still validate against the complete snapshot.
    assert sink.drafts[0].tags == [last_entry, "업무 운영"]


def test_approval_rules_contact_alias_filter_and_count_warning(tmp_path, caplog):
    from weaver.loom.summary_review import ApprovalExtractor

    context = ContextDictionary(tmp_path / "state")
    context.add_entry("김철수", "person")
    context.add_entry("가상연구소", "organization")

    class RulesLlm(FakeLlm):
        def generate_json(self, task, system, user, schema):
            for rule in (
                "검토 질문과 답 자체는 Thread로 만들지 않는다",
                "사람 동일성",
                "미확인",
                "직함·소속",
                "화자 본인",
                "오인식·오타·띄어쓰기",
                "corrections(observed → canonical)",
            ):
                assert rule in system
            assert json.loads(user)["contact_name"] == "홍길동"
            data = super().generate_json(task, system, user, schema).model_dump()
            data["body"] = "김철수가 가상연구소에 자료를 보냈다."
            data["corrections"] = []
            data["new_entries"] = [
                {
                    "name": "홍길동",
                    "kind": "person",
                    "aliases": ["홍 길동", "홍길동", "별명"],
                },
                {"name": "지원팀 김영희 팀장님", "kind": "person", "aliases": []},
            ]
            data["threads"][0]["tags"] = ["홍 길동", "지원팀 김영희 팀장님"]
            return fixture_response(schema, data)

    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["홍길동"],
        edited_body=None,
        feedback=None,
    )
    prepared = ApprovalExtractor(
        RulesLlm(), context, FakeSink(), contact_name="홍길동"
    ).prepare(package(), "합성 원문", verdict, now=NOW)
    assert prepared.new_entries[0].aliases == ["별명"]
    assert prepared.threads[0].tags == ["홍길동", "업무 운영"]
    assert "approved body has untagged dictionary names: 2" in caplog.text
    assert "김철수" not in caplog.text and "가상연구소" not in caplog.text


def test_open_requests_are_active_budgeted_ranked_and_validated(tmp_path, caplog):
    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.loom.reextract import ExtractionView
    from weaver.loom.summary_review import ApprovalExtractor

    context = ContextDictionary(tmp_path / "context")
    context.add_entry("자료", "topic")
    fabric = FabricStore(tmp_path / "fabric.db")
    base = ThreadDraft(
        thread_id="request",
        kind="요청",
        content="자료 전송",
        occurred_at=NOW,
        recorded_at=NOW,
        tags=["자료"],
        field=None,
        value=None,
        source_id=SOURCE,
        actor="human",
        direction="받음",
        resolves=None,
        retracts=None,
        correction=None,
    )
    rows = [
        base,
        base.model_copy(update={"thread_id": "done"}),
        base.model_copy(update={"thread_id": "gone"}),
        base.model_copy(
            update={
                "thread_id": "resolved",
                "kind": "결정",
                "direction": None,
                "resolves": "done",
            }
        ),
        base.model_copy(
            update={
                "thread_id": "retracted",
                "kind": "기록",
                "direction": None,
                "retracts": "gone",
            }
        ),
    ]
    rows.extend(
        base.model_copy(
            update={"thread_id": f"other-{i}", "tags": [], "content": "x" * 1000}
        )
        for i in range(70)
    )
    fabric.append(rows)

    class Llm(FakeLlm):
        target = "request"

        def generate_json(self, task, system, user, schema):
            payload = json.loads(user)
            assert "완료·응답·취소" in system and "유사성만으로" in system
            assert payload["open_requests"][0]["id"] == "request"
            ids = {r["id"] for r in payload["open_requests"]}
            assert "done" not in ids and "gone" not in ids
            assert (
                len(json.dumps(payload["open_requests"], ensure_ascii=False).encode())
                <= 8000
            )
            data = super().generate_json(task, system, user, schema).model_dump()
            data["threads"][0]["resolves"] = self.target
            return fixture_response(schema, data)

    llm = Llm()
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["홍길동"],
        edited_body=None,
        feedback=None,
    )
    extractor = ApprovalExtractor(llm, context, fabric)
    for target in ("request", "done", "gone", "resolved", "unknown"):
        llm.target = target
        prepared = extractor.prepare(package(), "자료", verdict, now=NOW)
        assert prepared.threads[0].resolves == (
            "request" if target == "request" else None
        )
    assert "dropped invalid request resolution" in caplog.text
    assert not any(
        t.kind == "요청"
        for t in ExtractionView(fabric.entries(ThreadQuery()), SOURCE).threads
    )


def test_contact_metadata_survives_restart_and_legacy_spinner_fallback(tmp_path):
    import sqlite3

    from weaver.loom.summary_state import SummaryState, read_contact_name

    state = SummaryState(tmp_path)
    state.save_package(package(), "합성 원문", "김철수")
    state.save_package(package(), "합성 원문")
    assert SummaryState(tmp_path).contact_name("pkg", SOURCE) == "김철수"
    with state.connect() as db:
        db.execute("DROP TABLE contacts")
    with sqlite3.connect(tmp_path / "spinner.sqlite3") as db:
        db.execute("CREATE TABLE results (source_id TEXT, version TEXT, source TEXT)")
        db.execute(
            "INSERT INTO results VALUES (?, ?, ?)",
            (SOURCE, "1", json.dumps({"hints": {"counterpart": "김영희"}})),
        )
    assert read_contact_name(tmp_path, "pkg", SOURCE) == "김영희"


@pytest.mark.parametrize("action", ["approve", "edit"])
def test_prompt_corrections_are_approval_only(tmp_path, action):
    class PromptLlm(FakeLlm):
        system = ""

        def generate_json(self, task, system, user, schema):
            self.system = system
            return super().generate_json(task, system, user, schema)

    llm = PromptLlm()
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
        action=action,
        answers=["홍길동"],
        edited_body="사람이 편집한 업무 사실" if action == "edit" else None,
        feedback=None,
    )
    review.apply(package(), "홍길돈 원문", verdict, now=NOW)
    assert (
        "오인식은 corrections(observed → canonical)로만 제안한다" in llm.system
    ) == (action == "approve")
    if action == "edit":
        assert "edit에서는 corrections를 비운다" in llm.system
        assert "canonical은 검토 답 하나와 정확히 일치" not in llm.system
    assert "#149" not in llm.system
    assert "counterpart가 있으면 그 표준 이름도 반드시 tags에 포함한다" in llm.system


def _schedule_review(tmp_path: Path, value: dict[str, object], seen: list[str]):
    class Llm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            seen.append(system)
            return fixture_response(
                schema,
                {
                    "body": "방문한다.",
                    "corrections": [],
                    "threads": [
                        {
                            "kind": "일정",
                            "content": "UPS 점검 방문 일정",
                            "occurred_at": NOW.isoformat(),
                            "tags": [],
                            "direction": None,
                            "resolves": None,
                            "retracts": None,
                            "field": "schedule",
                            "value": value,
                            "blocked_by_index": None,
                        }
                    ],
                },
            )

    sink = FakeSink()
    review = SummaryReview(
        Llm(),
        ContextDictionary(tmp_path / "state"),
        sink,
        tmp_path / "archive",
        state_root=tmp_path / "state",
        timezone="Asia/Seoul",
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
    return sink


def test_schedule_title_and_description_extracted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = {"start": "2026-01-03T09:00:00", "end": None, "timezone": None}
    description = "- 담당 홍길동\n- 2층 전산실"
    sink = _schedule_review(
        tmp_path / "a",
        base | {"title": "서버 정기점검 방문", "description": description},
        [],
    )
    assert sink.drafts[0].value is not None
    assert sink.drafts[0].value["title"] == "서버 정기점검 방문"
    assert sink.drafts[0].value["description"] == description
    with caplog.at_level("WARNING"):
        sink = _schedule_review(
            tmp_path / "b",
            base | {"title": "12월 3일 방문", "description": "  "},
            [],
        )
    assert sink.drafts[0].value is not None
    assert "title" not in sink.drafts[0].value
    assert "description" not in sink.drafts[0].value
    assert "dropped invalid schedule title" in caplog.text
    sink = _schedule_review(tmp_path / "c", base | {"description": "가" * 1500}, [])
    assert sink.drafts[0].value is not None
    assert len(sink.drafts[0].value["description"]) == 1000


def test_existing_non_schedule_event_thread_does_not_break_the_hint(
    tmp_path: Path,
) -> None:
    from weaver.fabric import FabricStore

    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append(
        [
            ThreadDraft(
                thread_id="due",
                kind="일정",
                content="보고서 마감",
                occurred_at=NOW,
                recorded_at=NOW,
                tags=[],
                field="deadline",
                value="2026-01-05",
                source_id=SOURCE,
                actor="ai_approved",
                direction=None,
                resolves=None,
                retracts=None,
                correction=None,
            )
        ]
    )
    seen: list[dict[str, object]] = []

    class Llm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            seen.append(json.loads(user))
            return fixture_response(
                schema,
                {
                    "body": "방문한다.",
                    "corrections": [],
                    "threads": [
                        {
                            "kind": "일정",
                            "content": "UPS 점검 방문",
                            "occurred_at": NOW.isoformat(),
                            "tags": [],
                            "direction": None,
                            "resolves": None,
                            "retracts": None,
                            "field": "schedule",
                            "value": {
                                "start": "2026-01-03T09:00:00",
                                "end": None,
                            },
                            "blocked_by_index": None,
                        }
                    ],
                },
            )

    review = SummaryReview(
        Llm(),
        ContextDictionary(tmp_path / "state"),
        fabric,
        tmp_path / "archive",
        state_root=tmp_path / "state",
        timezone="Asia/Seoul",
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
    (hint,) = seen[0]["existing_schedules"]  # type: ignore[misc]
    assert hint["title"] == "보고서 마감"  # type: ignore[index]


def _approve_verdict() -> Verdict:
    return Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action="approve",
        answers=["홍길동"],
        edited_body=None,
        feedback=None,
    )


def test_tree_prompt_and_fixture_validates(tmp_path: Path) -> None:
    from weaver.context.tree import expand

    seen: dict[str, str] = {}

    class TreeLlm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            seen["system"], seen["user"] = system, user
            return schema.model_validate(
                {
                    "new_entries": [],
                    "body": "라우터 패치를 결정했다.",
                    "corrections": [],
                    "threads": [
                        {
                            "kind": "결정",
                            "content": "라우터 패치 결정",
                            "occurred_at": NOW.isoformat(),
                            "tags": ["회사/infra/network/router"],
                            "field": None,
                            "value": None,
                            "direction": None,
                            "resolves": None,
                            "retracts": None,
                        }
                    ],
                }
            )

    sink = FakeSink()
    context = ContextDictionary(tmp_path / "state")
    context.sync_tree("회사", expand("회사", ["회사/infra/network/router"]))
    review = SummaryReview(
        TreeLlm(), context, sink, tmp_path / "archive", state_root=tmp_path / "state"
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
    review.approve(package(), "router 원문", verdict, now=NOW)
    assert "새 경로를 만들지 않는다" in seen["system"]
    assert "영역(area) 정확히 하나" not in seen["system"]
    assert "회사/infra/network/router" in json.loads(seen["user"])["paths"]
    assert sink.drafts[0].tags == ["회사/infra/network/router"]


def test_unknown_path_falls_back_and_proposes_once_after_apply(tmp_path: Path) -> None:
    from weaver.context.proposals import LocationProposals
    from weaver.context.tree import expand

    class UnknownPathLlm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            return schema.model_validate(
                {
                    "new_entries": [],
                    "body": "NAS 증설을 결정했다.",
                    "corrections": [],
                    "threads": [
                        {
                            "kind": "결정",
                            "content": "NAS 증설 결정",
                            "occurred_at": NOW.isoformat(),
                            "tags": ["회사/infra/storage/nas"],
                            "field": None,
                            "value": None,
                            "direction": None,
                            "resolves": None,
                            "retracts": None,
                        }
                    ],
                }
            )

    class FailingOnce(FakeSink):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def append(self, threads: list[ThreadDraft]) -> list[Thread]:
            if self.fail:
                self.fail = False
                raise OSError("fabric unavailable")
            return super().append(threads)

    sink = FailingOnce()
    context = ContextDictionary(tmp_path / "state")
    context.sync_tree("회사", expand("회사", ["회사/infra/network/router"]))
    store = LocationProposals(tmp_path / "state")
    review = SummaryReview(
        UnknownPathLlm(),
        context,
        sink,
        tmp_path / "archive",
        state_root=tmp_path / "state",
        location_proposals=store,
    )
    verdict = _approve_verdict()
    prepared = review.prepare(package(), "NAS 원문", verdict, now=NOW)
    assert prepared.threads[0].tags == ["회사/미분류"]
    assert store.pending() == []  # prepare writes nothing
    # A failed Fabric append must leave no proposal behind.
    with pytest.raises(OSError):
        review.approve(package(), "NAS 원문", verdict, now=NOW)
    assert store.pending() == []
    review.approve(package(), "NAS 원문", verdict, now=NOW)
    (item,) = store.pending()
    assert item.path == "회사/infra/storage/nas"
    assert item.parent == "회사/infra"
    assert item.fallback == "회사/미분류"
    assert item.reason == "unknown_path" and item.count == 1
    assert item.thread_ids == [sink.drafts[0].thread_id]
    assert item.source_ids == [SOURCE]
    # Replaying the same judgement is still one proposal.
    again = SummaryReview(
        UnknownPathLlm(),
        context,
        sink,
        tmp_path / "archive",
        state_root=tmp_path / "state",
        location_proposals=store,
    )
    again.approve(package(), "NAS 원문", verdict, now=NOW)
    assert len(store.pending()) == 1


def test_failing_location_proposal_does_not_skip_dictionary_learning(
    tmp_path: Path,
) -> None:
    from weaver.context.proposals import LocationProposals
    from weaver.context.tree import expand

    class UnknownPathLlm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            data = super().generate_json(task, system, user, schema).model_dump()
            data["threads"][0]["tags"] = ["회사/infra/storage/nas"]
            return fixture_response(schema, data)

    class BrokenProposals(LocationProposals):
        def propose(self, *args: object, **kwargs: object) -> None:
            raise OSError("proposals unavailable")

    sink = FakeSink()
    context = ContextDictionary(tmp_path / "state")
    context.sync_tree("회사", expand("회사", ["회사/infra/network/router"]))
    review = SummaryReview(
        UnknownPathLlm(),
        context,
        sink,
        tmp_path / "archive",
        state_root=tmp_path / "state",
        location_proposals=BrokenProposals(tmp_path / "state"),
    )
    verdict = _approve_verdict()
    review.approve(package(), "홍길돈 원문", verdict, now=NOW)
    assert len(sink.drafts) == 1
    assert [c.name for c in context.candidates("홍길돈")] == ["홍길동"]


def test_five_threads_from_one_source_yield_five_drafts(tmp_path: Path) -> None:
    kinds = ["요청", "요청", "기록", "기록", "결정"]

    class Llm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            return fixture_response(
                schema,
                {
                    "body": "ACME 통화 정리",
                    "corrections": [],
                    "threads": [
                        {
                            "kind": kind,
                            "content": f"ACME 건 {i} 처리",
                            "occurred_at": NOW.isoformat(),
                            "tags": [],
                            "field": None,
                            "value": None,
                            "direction": "받음" if kind == "요청" else None,
                            "resolves": None,
                            "retracts": None,
                            "blocked_by_index": None,
                        }
                        for i, kind in enumerate(kinds)
                    ],
                },
            )

    sink = FakeSink()
    review = SummaryReview(
        Llm(),
        ContextDictionary(tmp_path / "state"),
        sink,
        tmp_path / "archive",
        state_root=tmp_path / "state",
    )
    review.approve(package(), "원문", _approve_verdict(), now=NOW)
    assert [d.kind for d in sink.drafts] == kinds
