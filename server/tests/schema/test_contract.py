"""Consumer contract tests: fixture files are also the Android minimum examples."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from weaver.schema import (
    SHARED_MODELS,
    ApprovedSchedule,
    EventSummary,
    Question,
    ReviewPackage,
    Script,
    Source,
    Thread,
    ThreadDraft,
    Verdict,
)

ROOT = Path(__file__).resolve().parents[3]
VERSIONED = [m for m in SHARED_MODELS if "schema_version" in m.model_fields]
CAPTURE = {"Manifest", "Receipt", "Source", "Script"}


def example(name):
    return json.loads((ROOT / "schema" / "examples" / f"{name}.json").read_text())


def model_named(name):
    return next(m for m in SHARED_MODELS if m.__name__ == name)


def expected_version(model):
    return 1 if model.__name__ in CAPTURE else 2


@pytest.mark.parametrize("model", SHARED_MODELS, ids=lambda m: m.__name__)
def test_examples_round_trip(model):
    payload = example(model.__name__)
    parsed = model.model_validate(payload)
    assert model.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("model", VERSIONED, ids=lambda m: m.__name__)
@pytest.mark.parametrize("version", [0, 1, 2, 3, True, 2.0, "2"])
def test_only_the_models_own_integer_version_is_accepted(model, version):
    payload = example(model.__name__) | {"schema_version": version}
    if type(version) is int and version == expected_version(model):
        assert model.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.parametrize("model", SHARED_MODELS, ids=lambda m: m.__name__)
def test_unknown_keys_are_rejected(model):
    with pytest.raises(ValidationError):
        model.model_validate(example(model.__name__) | {"unknown": 1})


@pytest.mark.parametrize("model", SHARED_MODELS, ids=lambda m: m.__name__)
def test_all_top_level_keys_are_required_even_if_nullable(model):
    payload = example(model.__name__)
    defaults = (
        {"blocked_by", "revision", "retracted"}
        if model
        in (
            ApprovedSchedule,
            Thread,
            ThreadDraft,
        )
        else set()
    )
    for key in payload:
        if key in defaults:
            continue
        broken = dict(payload)
        del broken[key]
        with pytest.raises(ValidationError):
            model.model_validate(broken)


@pytest.mark.parametrize(
    ("model_name", "field", "bad"),
    [
        ("Manifest", "capture_id", "cap_20261107_1"),
        ("Receipt", "source_id", "ent_01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("Alert", "alert_id", "alt_Z1ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("Source", "channel", "photo"),
        ("Script", "source_id", "src_bad"),
        ("Manifest", "captured_at", "2026-11-07T12:00:00"),
        ("Receipt", "received_at", "2026-11-07T12:00:00"),
        ("Alert", "created_at", "2026-11-07T12:00:00"),
        ("Thread", "occurred_at", "2026-11-07T12:00:00"),
        ("Thread", "recorded_at", "2026-11-07"),
        ("Script", "occurred_at", 1794030000),
        ("Source", "payload_ref", "/Users/private/audio.m4a"),
        ("Source", "payload_ref", "../secret"),
        ("Verdict", "revision", 0),
        ("Verdict", "action", "revise"),
        ("ReviewPackage", "revision", True),
        ("Thread", "kind", "자동실행"),
        ("EventSummary", "summary_id", "wrong"),
        ("ApprovedSchedule", "summary_id", "bad"),
    ],
)
def test_invalid_values(model_name, field, bad):
    payload = example(model_name)
    payload[field] = bad
    with pytest.raises(ValidationError):
        model_named(model_name).model_validate(payload)


@pytest.mark.parametrize("name", ["Script-text", "Script-kakao", "Script-audio"])
def test_script_ranges_preserve_text(name):
    script = Script.model_validate(example(name))
    for segment in script.segments:
        assert script.text[segment.start_char : segment.end_char]
    assert script.source_id == example("Source")["source_id"]


def test_script_invalid_range_and_audio_pair():
    payload = example("Script")
    payload["segments"][0]["end_char"] = len(payload["text"]) + 1
    with pytest.raises(ValidationError):
        Script.model_validate(payload)
    payload = example("Script-audio")
    payload["segments"][0]["start_ms"] = None
    with pytest.raises(ValidationError):
        Script.model_validate(payload)


@pytest.mark.parametrize("kind", ["text", "kakao", "audio"])
def test_source_script_pair_preserves_metadata(kind):
    source = Source.model_validate(example(f"Source-{kind}"))
    script = Script.model_validate(example(f"Script-{kind}"))
    assert (source.source_id, source.channel, source.occurred_at) == (
        script.source_id,
        script.channel,
        script.occurred_at,
    )
    if kind == "kakao":
        assert all(s.speaker and s.occurred_at for s in script.segments)
    elif kind == "audio":
        assert all(
            s.start_ms is not None and s.end_ms is not None for s in script.segments
        )
    else:
        assert all(s.start_ms is None and s.end_ms is None for s in script.segments)


@pytest.fixture
def draft():
    return {
        "thread_id": "thread-1",
        "kind": "기록",
        "content": "메모",
        "occurred_at": "2026-01-01T00:00:00Z",
        "recorded_at": "2026-01-01T00:00:00Z",
        "tags": [],
        "field": None,
        "value": None,
        "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "actor": "human",
        "direction": None,
        "resolves": None,
        "retracts": None,
        "correction": None,
    }


def test_direction_is_required_only_for_requests(draft):
    assert ThreadDraft.model_validate(draft | {"kind": "요청", "direction": "받음"})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"kind": "요청"})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"direction": "보냄"})


def test_correction_only_for_tag_correction(draft):
    correction = {"target": "thread-2", "remove": ["옛태그"], "add": ["새태그"]}
    assert ThreadDraft.model_validate(
        draft | {"kind": "태그정정", "correction": correction}
    )
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"kind": "태그정정"})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"correction": correction})


def test_resolves_only_on_decision(draft):
    assert ThreadDraft.model_validate(draft | {"kind": "결정", "resolves": "thread-2"})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"resolves": "thread-2"})


def test_rejection_has_no_content(draft):
    assert ThreadDraft.model_validate(draft | {"kind": "거절", "content": None})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"kind": "거절"})


def test_sequence_is_assigned_by_sink(draft):
    assert Thread.model_validate(draft | {"seq": 1}).seq == 1
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"seq": 1})
    with pytest.raises(ValidationError):
        Thread.model_validate(draft | {"seq": 0})


def test_question_has_exactly_three_choices():
    data = {
        "question": "어느 쪽인가요?",
        "rationale": "모호함",
        "choices": ["가", "나", "다"],
    }
    assert Question.model_validate(data)
    for choices in ([], ["가", "나"], ["가", "나", "다", "라"]):
        with pytest.raises(ValidationError):
            Question.model_validate(data | {"choices": choices})


def test_review_package_question_limit():
    data = example("ReviewPackage")
    question = data["questions"][0]
    assert ReviewPackage.model_validate(data | {"questions": [question] * 3})
    with pytest.raises(ValidationError):
        ReviewPackage.model_validate(data | {"questions": [question] * 4})


def test_verdict_action_requires_appropriate_input():
    data = example("Verdict")
    assert Verdict.model_validate(data)
    assert Verdict.model_validate(data | {"action": "edit", "edited_body": "수정"})
    assert Verdict.model_validate(
        data | {"action": "feedback", "feedback": "더 자세히"}
    )
    assert Verdict.model_validate(data | {"action": "reject"})
    for invalid in (
        {"action": "edit"},
        {"action": "feedback"},
        {"feedback": "더 자세히"},
        {"action": "reject", "edited_body": "수정"},
    ):
        with pytest.raises(ValidationError):
            Verdict.model_validate(data | invalid)


def test_event_summary_is_content_addressed():
    data = example("EventSummary")
    for invalid in ({"summary_id": "wrong"}, {"replaces": "wrong"}):
        with pytest.raises(ValidationError):
            EventSummary.model_validate(data | invalid)


def test_schedule_value_and_block_reference(draft):
    schedule = {
        "start": "2026-01-01T09:00:00+09:00",
        "end": None,
        "timezone": "Asia/Seoul",
    }
    assert ThreadDraft.model_validate(
        draft
        | {
            "kind": "일정",
            "field": "schedule",
            "value": schedule,
            "blocked_by": "request-1",
        }
    )
    for invalid in (
        "2026-01-01",
        {},
        schedule | {"timezone": "No/Such_Zone"},
        schedule | {"start": "2026-01-01T09:00:00"},
        schedule | {"end": "2025-01-01T00:00:00Z"},
    ):
        with pytest.raises(ValidationError):
            ThreadDraft.model_validate(draft | {"field": "schedule", "value": invalid})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"blocked_by": "request-1"})


def test_approved_schedule_contract():
    data = example("ApprovedSchedule")
    for invalid in (
        {"end": "2025-01-01T00:00:00Z"},
        {"title": " "},
        {"timezone": "x/y"},
    ):
        with pytest.raises(ValidationError):
            ApprovedSchedule.model_validate(data | invalid)
