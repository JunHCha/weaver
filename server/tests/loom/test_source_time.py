"""Relative dates in a source are resolved against when the source happened.

A delayed approval, a replay and a re-extraction preview all use the persisted
source time (not the processing clock) as "today" in the source's local zone
(KST). Deadlines and schedules that already passed at that time are dropped;
zoneless values are read in the configured zone. Occurrence times the model
guesses outside the plausible window are corrected, and future ones never
stored. When only a capture time is known, the reference is marked as an
estimate and historical dates in imported transcripts are preserved. Synthetic
approvals only; no external models or vaults."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from server.tests.fake_responses import fixture_response
from server.tests.loom.test_reextract import hashes, seed
from server.tests.loom.test_summary_review import FakeSink, package

from weaver.context import ContextDictionary
from weaver.loom.reextract import reextract, render
from weaver.loom.summary import SummaryDrafts
from weaver.loom.summary_review import SummaryReview
from weaver.loom.summary_state import SummaryState, read_source_occurred_at
from weaver.schema import Verdict

SOURCE_TIME = datetime.fromisoformat("2026-11-12T10:30:00+09:00")
PROCESS_TIME = datetime(2026, 11, 17, tzinfo=UTC)


class RelativeLlm:
    def __init__(self, *, occurrence=None, field="deadline", value=None):
        self.occurrence = occurrence
        self.field = field
        self.value = value
        self.inputs = []

    def generate_json(self, task, system, user, schema):
        if task == "loom.reextract_bootstrap":
            return schema.model_validate({"entries": []})
        payload = json.loads(user)
        self.inputs.append(payload)
        reference = datetime.fromisoformat(payload["source_occurred_at"])
        return fixture_response(
            schema,
            {
                "body": "오늘 내 등록",
                "corrections": [],
                "threads": [
                    {
                        "kind": "기록",
                        "content": "오늘 내 등록",
                        "tags": [],
                        "occurred_at": (self.occurrence or reference).isoformat(),
                        "field": self.field,
                        "value": self.value
                        if self.value is not None
                        else payload["today"],
                        "direction": None,
                        "resolves": None,
                        "retracts": None,
                    }
                ],
            },
        )

    def generate_text(self, *args):
        raise AssertionError("unexpected text call")


def verdict(action="approve"):
    return Verdict(
        schema_version=2,
        package_id="pkg",
        revision=1,
        action=action,
        answers=["홍길동"],
        feedback=None,
        edited_body="오늘 내 등록" if action == "edit" else None,
    )


@pytest.mark.parametrize("action", ["approve", "edit"])
def test_delayed_approval_uses_persisted_source_time_and_replays(tmp_path, action):
    state = SummaryState(tmp_path)
    state.save_package(package(), "오늘 내 등록", source_occurred_at=SOURCE_TIME)
    sink = FakeSink()
    llm = RelativeLlm()
    review = SummaryReview(
        llm,
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    )
    review.apply(package(), "오늘 내 등록", verdict(action), now=PROCESS_TIME)
    (thread,) = sink.drafts
    assert thread.occurred_at == SOURCE_TIME
    assert thread.recorded_at == PROCESS_TIME
    assert thread.value == "2026-11-12"
    review.apply(
        package(), "오늘 내 등록", verdict(action), now=PROCESS_TIME + timedelta(days=1)
    )
    assert len(llm.inputs) == 1 and len(sink.drafts) == 1


@pytest.mark.parametrize(
    "occurrence", [PROCESS_TIME, SOURCE_TIME - timedelta(days=367)]
)
def test_occurrence_guesses_corrected_without_logging_content(
    tmp_path, caplog, occurrence
):
    SummaryState(tmp_path).save_package(
        package(), "원문", source_occurred_at=SOURCE_TIME
    )
    sink = FakeSink()
    SummaryReview(
        RelativeLlm(occurrence=occurrence),
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    ).apply(package(), "원문", verdict(), now=PROCESS_TIME)
    assert sink.drafts[0].occurred_at == SOURCE_TIME
    assert "corrected out-of-range occurrence time" in caplog.text
    assert "오늘 내 등록" not in caplog.text


@pytest.mark.parametrize(
    ("field", "value", "retained"),
    [
        ("deadline", "2026-11-11", False),
        ("deadline", "2026-11-12T09:00:00+09:00", False),
        ("deadline", "unparseable", False),
        ("deadline", "2026-11-13", True),
        ("schedule", {"start": "2026-11-12T09:00:00", "end": None}, False),
        ("schedule", {"start": "2026-11-13T09:00:00", "end": None}, True),
    ],
)
def test_temporal_values_compare_source_not_processing_clock(
    tmp_path, field, value, retained
):
    SummaryState(tmp_path).save_package(
        package(), "원문", source_occurred_at=SOURCE_TIME
    )
    sink = FakeSink()
    SummaryReview(
        RelativeLlm(field=field, value=value),
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    ).apply(package(), "원문", verdict(), now=PROCESS_TIME)
    assert (sink.drafts[0].value is not None) == retained
    assert sink.drafts[0].field == (field if retained else None)


@pytest.mark.parametrize("occurred", [None, SOURCE_TIME.isoformat()])
def test_legacy_spinner_recovers_occurrence_or_capture_readonly(tmp_path, occurred):
    SummaryState(tmp_path).save_package(package(), "원문")
    with sqlite3.connect(tmp_path / "spinner.sqlite3") as db:
        db.execute("CREATE TABLE results (source_id TEXT, version TEXT, source TEXT)")
        db.execute(
            "INSERT INTO results VALUES (?, '1', ?)",
            (
                package().source_id,
                json.dumps(
                    {
                        "occurred_at": occurred,
                        "captured_at": SOURCE_TIME.isoformat(),
                    }
                ),
            ),
        )
    before = hashes(tmp_path)
    assert read_source_occurred_at(tmp_path, package()) == SOURCE_TIME
    assert hashes(tmp_path) == before


def test_draft_saves_source_time_without_changing_phone_package(tmp_path):
    class DraftLlm:
        def generate_json(self, task, system, user, schema):
            return schema.model_validate(
                {"script": "원문", "body": "오늘 내 등록", "questions": []}
            )

    draft, _ = SummaryDrafts(
        DraftLlm(),
        ContextDictionary(tmp_path),
        tmp_path / "proposals",
        state_root=tmp_path,
    ).create(
        package().source_id, "원문", source_occurred_at=SOURCE_TIME, now=PROCESS_TIME
    )
    assert read_source_occurred_at(tmp_path, draft) == SOURCE_TIME
    assert "source_occurred_at" not in draft.model_dump()


@pytest.mark.parametrize("storage", ["metadata", "spinner"])
def test_synthetic_reextract_preview_reproduces_source_reference(tmp_path, storage):
    seed(tmp_path, body="오늘 내 등록")
    state = SummaryState(tmp_path)
    saved, script = state.load_package("pkg", 1)
    if storage == "metadata":
        state.save_package(saved, script, source_occurred_at=SOURCE_TIME)
    else:
        with sqlite3.connect(tmp_path / "spinner.sqlite3") as db:
            db.execute(
                "CREATE TABLE results (source_id TEXT, version TEXT, source TEXT)"
            )
            db.execute(
                "INSERT INTO results VALUES (?, '1', ?)",
                (
                    saved.source_id,
                    json.dumps(
                        {
                            "occurred_at": SOURCE_TIME.isoformat(),
                        }
                    ),
                ),
            )
    before = hashes(tmp_path)
    report = reextract(tmp_path, tmp_path / "wiki", RelativeLlm())
    assert not report.errors and not report.sources[0].error
    (thread,) = report.sources[0].new
    assert thread.occurred_at == SOURCE_TIME
    assert thread.recorded_at > SOURCE_TIME
    assert thread.value == "2026-11-12"
    assert hashes(tmp_path) == before
    preview = render(report)
    assert "occurred_at=2026-11-12T10:30:00+09:00" in preview


def test_reference_metadata_is_immutable_and_legacy_fallback_is_stable(tmp_path):
    state = SummaryState(tmp_path)
    state.save_package(package(), "원문")
    assert read_source_occurred_at(tmp_path, package()) == min(
        package().stage_times.values()
    )
    state.save_package(package(), "원문", source_occurred_at=SOURCE_TIME)
    with pytest.raises(ValueError, match="conflicting source reference time"):
        state.save_package(package(), "원문", source_occurred_at=PROCESS_TIME)
    assert read_source_occurred_at(tmp_path, package()) == SOURCE_TIME
    with pytest.raises(ValueError, match="missing source reference time"):
        read_source_occurred_at(
            tmp_path,
            package().model_copy(update={"package_id": "missing", "stage_times": {}}),
        )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        # Any non-string deadline and any non-object schedule take one branch.
        ("deadline", 42),
        ("schedule", "2026-11-13"),
    ],
)
def test_malformed_temporal_value_does_not_stop_approval(
    tmp_path, caplog, field, bad_value
):
    class MalformedLlm(RelativeLlm):
        def generate_json(self, task, system, user, schema):
            response = super().generate_json(task, system, user, schema)
            # Exercise defensive server validation even for clients bypassing
            # Pydantic's transport validation (e.g. model_construct adapters).
            thread = response.threads[0].model_copy(
                update={"field": field, "value": bad_value}
            )
            return response.model_copy(update={"threads": [thread]})

    SummaryState(tmp_path).save_package(
        package(), "원문", source_occurred_at=SOURCE_TIME
    )
    sink = FakeSink()
    review = SummaryReview(
        MalformedLlm(),
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    )
    review.apply(package(), "원문", verdict(), now=PROCESS_TIME)
    assert sink.drafts[0].field is None and sink.drafts[0].value is None
    assert "dropped invalid" in caplog.text
    assert "오늘 내 등록" not in caplog.text


@pytest.mark.parametrize(
    ("storage", "field", "value"),
    [
        # Each storage is read by its own branch; the historical-value policy of
        # the two fields is the same for every storage, so schedule runs once.
        ("captured_metadata", "deadline", "2020-02-02"),
        ("captured_spinner", "deadline", "2020-02-02"),
        ("stages", "deadline", "2020-02-02"),
        ("stages", "schedule", {"start": "2020-02-02T09:00:00", "end": None}),
    ],
)
def test_fallback_keeps_imported_history_in_approval_and_preview(
    tmp_path, caplog, storage, field, value
):
    from weaver.loom.summary_state import read_source_reference

    seed(tmp_path)
    state = SummaryState(tmp_path)
    saved, script = state.load_package("pkg", 1)
    if storage == "captured_metadata":
        state.save_package(
            saved, script, source_occurred_at=SOURCE_TIME, source_time_is_fallback=True
        )
    elif storage == "captured_spinner":
        with sqlite3.connect(tmp_path / "spinner.sqlite3") as db:
            db.execute(
                "CREATE TABLE results (source_id TEXT, version TEXT, source TEXT)"
            )
            db.execute(
                "INSERT INTO results VALUES (?, '1', ?)",
                (
                    saved.source_id,
                    json.dumps(
                        {
                            "occurred_at": None,
                            "captured_at": SOURCE_TIME.isoformat(),
                        }
                    ),
                ),
            )
    assert read_source_reference(tmp_path, saved).is_fallback
    llm = RelativeLlm(
        occurrence=datetime(2020, 2, 1, tzinfo=UTC), field=field, value=value
    )
    # Planning and ordinary approval share the same historical-value policy.
    report = reextract(tmp_path, tmp_path / "wiki", llm)
    assert not report.sources[0].error
    (thread,) = report.sources[0].new
    assert thread.occurred_at == datetime(2020, 2, 1, tzinfo=UTC)
    assert thread.field == field and thread.value is not None
    assert "출처 시각 추정" in render(report)
    sink = FakeSink()
    approval_root = tmp_path / "approval"
    reference = read_source_reference(tmp_path, saved)
    SummaryState(approval_root).save_package(
        saved,
        script,
        source_occurred_at=reference.occurred_at,
        source_time_is_fallback=reference.is_fallback,
    )
    SummaryReview(
        llm,
        ContextDictionary(approval_root),
        sink,
        approval_root / "summaries",
        state_root=approval_root,
    ).apply(
        saved, script, verdict().model_copy(update={"answers": []}), now=PROCESS_TIME
    )
    assert sink.drafts[0].occurred_at == thread.occurred_at
    assert sink.drafts[0].value == thread.value
    assert "preserving historical extracted dates" in caplog.text
    if storage == "stages":
        assert "using estimated source time from review stages" in caplog.text
    assert "오늘 내 등록" not in caplog.text


@pytest.mark.parametrize(
    ("delta", "expected_delta"),
    [
        (timedelta(hours=12), timedelta(hours=12)),
        (timedelta(days=1), timedelta(days=1)),
        (timedelta(days=1, seconds=1), timedelta()),
    ],
)
def test_long_call_occurrences_keep_order_within_one_day(
    tmp_path, delta, expected_delta
):
    SummaryState(tmp_path).save_package(
        package(), "원문", source_occurred_at=SOURCE_TIME
    )
    sink = FakeSink()
    SummaryReview(
        RelativeLlm(occurrence=SOURCE_TIME + delta),
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    ).apply(package(), "원문", verdict(), now=PROCESS_TIME)
    assert sink.drafts[0].occurred_at == SOURCE_TIME + expected_delta


def test_future_phone_reference_never_stores_future_occurrence(tmp_path, caplog):
    fallback = True
    future = PROCESS_TIME + timedelta(days=2)
    SummaryState(tmp_path).save_package(
        package(), "원문", source_occurred_at=future, source_time_is_fallback=fallback
    )
    sink = FakeSink()
    SummaryReview(
        RelativeLlm(),
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    ).apply(package(), "원문", verdict(), now=PROCESS_TIME)
    assert sink.drafts[0].occurred_at == PROCESS_TIME
    assert "corrected future occurrence time" in caplog.text


def test_server_now_caps_long_call_tolerance(tmp_path):
    SummaryState(tmp_path).save_package(
        package(), "원문", source_occurred_at=SOURCE_TIME
    )
    sink = FakeSink()
    SummaryReview(
        RelativeLlm(occurrence=SOURCE_TIME + timedelta(hours=3)),
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    ).apply(package(), "원문", verdict(), now=SOURCE_TIME + timedelta(hours=2))
    assert sink.drafts[0].occurred_at == SOURCE_TIME


def test_utc_source_crossing_kst_date_uses_local_today(tmp_path):
    reference = datetime.fromisoformat("2026-11-11T16:30:00+00:00")
    SummaryState(tmp_path).save_package(
        package(), "오늘 내 등록", source_occurred_at=reference
    )
    sink = FakeSink()
    llm = RelativeLlm()
    SummaryReview(
        llm,
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    ).apply(package(), "오늘 내 등록", verdict(), now=PROCESS_TIME)
    assert llm.inputs[0]["today"] == "2026-11-12"
    assert llm.inputs[0]["source_occurred_at"] == "2026-11-12T01:30:00+09:00"
    assert sink.drafts[0].value == "2026-11-12"


@pytest.mark.parametrize(
    ("field", "hour", "retained"),
    [
        ("deadline", "09:00:00", False),
        ("deadline", "11:00:00", True),
        ("schedule", "09:00:00", False),
        ("schedule", "11:00:00", True),
    ],
)
def test_zoneless_temporal_values_use_configured_local_zone(
    tmp_path, field, hour, retained
):
    value = "2026-11-12T" + hour
    llm = RelativeLlm(
        field=field,
        value=value if field == "deadline" else {"start": value, "end": None},
    )
    SummaryState(tmp_path).save_package(
        package(), "원문", source_occurred_at=SOURCE_TIME
    )
    sink = FakeSink()
    SummaryReview(
        llm,
        ContextDictionary(tmp_path),
        sink,
        tmp_path / "summaries",
        state_root=tmp_path,
    ).apply(package(), "원문", verdict(), now=PROCESS_TIME)
    assert (sink.drafts[0].value is not None) == retained
    if retained and field == "schedule":
        assert sink.drafts[0].value["start"] == value + "+09:00"


def test_past_schedule_drops_its_existing_update_link(tmp_path):
    from server.tests.loom.test_reextract import draft

    from weaver.fabric import FabricStore

    class UpdatingLlm(RelativeLlm):
        def generate_json(self, task, system, user, schema):
            response = super().generate_json(task, system, user, schema)
            thread = response.threads[0].model_copy(
                update={"kind": "일정", "updates": "existing"}
            )
            return response.model_copy(update={"threads": [thread]})

    SummaryState(tmp_path).save_package(
        package(), "원문", source_occurred_at=SOURCE_TIME
    )
    store = FabricStore(tmp_path / "fabric.sqlite3")
    store.append(
        [
            draft(
                "existing",
                kind="일정",
                field="schedule",
                value={
                    "start": "2026-11-13T09:00:00+09:00",
                    "end": None,
                    "timezone": "Asia/Seoul",
                },
            )
        ]
    )
    SummaryReview(
        UpdatingLlm(
            field="schedule", value={"start": "2026-11-12T09:00:00", "end": None}
        ),
        ContextDictionary(tmp_path),
        store,
        tmp_path / "summaries",
        state_root=tmp_path,
    ).apply(package(), "원문", verdict(), now=PROCESS_TIME)
    from weaver.fabric import ThreadQuery

    thread = store.entries(ThreadQuery())[-1]
    assert thread.updates is None and thread.field is None and thread.value is None


@pytest.mark.parametrize("has_occurrence", [True, False])
def test_summary_processor_stores_occurrence_or_capture_with_provenance(
    tmp_path, has_occurrence
):
    import threading

    from server.tests.test_call_single_pass import Llm, Store, source

    from weaver.loom.summary_state import read_source_reference
    from weaver.runtime.gate import LlmGate, LlmStatus
    from weaver.runtime.summary import SummaryProcessor
    from weaver.schema import ReviewPackage
    from weaver.spinner import Spinner

    src = source("share_text", "합성 메모").model_copy(
        update={
            "occurred_at": SOURCE_TIME if has_occurrence else None,
            "captured_at": PROCESS_TIME,
        }
    )
    llm = Llm()
    spinner = Spinner(
        Store(tmp_path, src, "합성 메모"),
        tmp_path / "state",
        configuration_version="test",
    )
    drafts = SummaryDrafts(
        llm,
        ContextDictionary(tmp_path / "state"),
        tmp_path / "proposals",
        state_root=tmp_path / "state",
    )
    processor = SummaryProcessor(
        spinner, drafts, LlmGate(lambda: LlmStatus(True, "ok"))
    )
    assert processor.process(src, threading.Event()) == "done"
    saved = ReviewPackage.model_validate_json(
        next(drafts.proposals.glob("*.r1.json")).read_bytes()
    )
    reference = read_source_reference(tmp_path / "state", saved)
    assert reference.occurred_at == (SOURCE_TIME if has_occurrence else PROCESS_TIME)
    assert reference.is_fallback == (not has_occurrence)


def test_old_time_metadata_without_provenance_is_conservatively_estimated(tmp_path):
    from weaver.loom.summary_state import read_source_reference

    SummaryState(tmp_path).save_package(package(), "원문")
    with sqlite3.connect(tmp_path / "summary.sqlite3") as db:
        db.execute("DROP TABLE source_times")
        db.execute(
            "CREATE TABLE source_times (id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO source_times VALUES ('pkg', ?)", (SOURCE_TIME.isoformat(),)
        )
    before = hashes(tmp_path)
    assert read_source_reference(tmp_path, package()).is_fallback
    assert hashes(tmp_path) == before
    SummaryState(tmp_path)
    assert read_source_reference(tmp_path, package()).is_fallback


def test_direct_extractor_stage_fallback_warns_and_preserves_history(tmp_path, caplog):
    from weaver.loom.summary_review import ApprovalExtractor

    llm = RelativeLlm(occurrence=datetime(2020, 2, 1, tzinfo=UTC), value="2020-02-02")
    prepared = ApprovalExtractor(llm, ContextDictionary(tmp_path), FakeSink()).prepare(
        package(), "2020년 2월 1일 대화\n내일 마감", verdict(), now=PROCESS_TIME
    )
    assert prepared.threads[0].occurred_at == datetime(2020, 2, 1, tzinfo=UTC)
    assert prepared.threads[0].value == "2020-02-02"
    assert llm.inputs[0]["source_time_is_fallback"] is True
    assert "using estimated source time from review stages" in caplog.text
