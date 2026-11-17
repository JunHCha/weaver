"""Synthetic delayed approvals and replay; no external models or vaults."""

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
        assert payload["today"] == "2026-11-12"
        assert payload["timezone"] == "Asia/Seoul"
        assert "오늘·내일·다음 주·18일" in system
        assert "근거가 없으면 source_occurred_at" in system
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
                        "value": self.value or reference.date().isoformat(),
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
    print(preview)


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
