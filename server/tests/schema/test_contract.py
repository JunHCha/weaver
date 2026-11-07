"""Consumer contract tests: fixture files are also the Android minimum examples."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from weaver.schema import SHARED_MODELS, Proposal, Thread, Verdict

ROOT = Path(__file__).resolve().parents[3]


def example(name):
    return json.loads((ROOT / "schema" / "examples" / f"{name}.json").read_text())


@pytest.mark.parametrize("model", SHARED_MODELS, ids=lambda m: m.__name__)
def test_examples_round_trip(model):
    payload = example(model.__name__)
    parsed = model.model_validate(payload)
    assert model.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("model", SHARED_MODELS, ids=lambda m: m.__name__)
@pytest.mark.parametrize("mutation", ["version", "missing", "extra"])
def test_file_contract_rejects_invalid_envelopes(model, mutation):
    payload = example(model.__name__)
    if mutation == "version":
        payload["schema_version"] = 2
    elif mutation == "missing":
        del payload[next(k for k in payload if k != "schema_version")]
    else:
        payload["unknown"] = 1
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("model_name", "field", "bad"),
    [
        ("Manifest", "capture_id", "cap_20261107_1"),
        ("Receipt", "source_id", "ent_01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("Proposal", "rev", 0),
        ("Proposal", "rev", True),
        ("Verdict", "rev", 0),
        ("Verdict", "verdict", "decision"),
        ("Alert", "alert_id", "alt_Z1ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("Entity", "type", "decision"),
        ("Relation", "type", "결정됨"),
        ("Entry", "kind", "자동실행"),
        ("Source", "channel", "photo"),
        ("Thread", "source_id", "src_bad"),
        ("Manifest", "captured_at", "2026-11-07T12:00:00"),
        ("Entry", "recorded_at", "2026-11-07"),
        ("Thread", "occurred_at", 1794030000),
        ("Source", "payload_ref", "/Users/private/audio.m4a"),
        ("Source", "payload_ref", "../secret"),
        ("Manifest", "schema_version", True),
    ],
)
def test_invalid_values(model_name, field, bad):
    model = next(m for m in SHARED_MODELS if m.__name__ == model_name)
    payload = example(model_name)
    payload[field] = bad
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("verdict", "edits", "reason", "instruction"),
    [
        ("approve", [], None, None),
        ("edit", [{"path": "/summary", "value": "수정"}], None, None),
        ("revise", [], None, "이름을 다시 확인"),
        ("reject", [], "wrong_person", None),
    ],
)
def test_verdict_branches(verdict, edits, reason, instruction):
    payload = example("Verdict") | dict(
        verdict=verdict, edits=edits, reject_reason=reason, instruction=instruction
    )
    assert Verdict.model_validate(payload).verdict == verdict
    broken = copy.deepcopy(payload)
    if verdict == "edit":
        broken["edits"] = []
    elif verdict == "revise":
        broken["instruction"] = "   "
    elif verdict == "reject":
        broken["reject_reason"] = None
    else:
        broken["instruction"] = "do something"
    with pytest.raises(ValidationError):
        Verdict.model_validate(broken)


@pytest.mark.parametrize("path", ["schedules[0].start", "/bad~2escape"])
def test_pointer_syntax(path):
    payload = example("Verdict") | dict(
        verdict="edit", edits=[{"path": path, "value": 1}]
    )
    with pytest.raises(ValidationError):
        Verdict.model_validate(payload)


def test_proposal_rejects_dangling_reference_and_unblocked_request():
    payload = example("Proposal")
    payload["entry_drafts"][0]["entities"] = ["missing"]
    with pytest.raises(ValidationError):
        Proposal.model_validate(payload)
    payload = example("Proposal")
    payload["schedules"][0]["blocked_by_request"] = None
    with pytest.raises(ValidationError):
        Proposal.model_validate(payload)


@pytest.mark.parametrize("name", ["Thread-text", "Thread-kakao", "Thread-audio"])
def test_thread_ranges_preserve_text(name):
    thread = Thread.model_validate(example(name))
    for segment in thread.segments:
        assert thread.text[segment.start_char : segment.end_char]
    assert thread.source_id == example("Source")["source_id"]


def test_thread_invalid_range_and_audio_pair():
    payload = example("Thread")
    payload["segments"][0]["end_char"] = len(payload["text"]) + 1
    with pytest.raises(ValidationError):
        Thread.model_validate(payload)
    payload = example("Thread-audio")
    payload["segments"][0]["end_ms"] = None
    with pytest.raises(ValidationError):
        Thread.model_validate(payload)


@pytest.mark.parametrize("model", SHARED_MODELS, ids=lambda m: m.__name__)
def test_all_top_level_keys_are_required_even_if_nullable(model):
    payload = example(model.__name__)
    for key in payload:
        broken = dict(payload)
        del broken[key]
        with pytest.raises(ValidationError):
            model.model_validate(broken)


@pytest.mark.parametrize("model", SHARED_MODELS, ids=lambda m: m.__name__)
def test_every_core_model_rejects_bad_identifiers(model):
    payload = example(model.__name__)
    key = next(k for k in payload if k.endswith("_id") or k == "from")
    payload[key] = "invalid_id"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "model_name,field",
    [
        ("Manifest", "captured_at"),
        ("Receipt", "received_at"),
        ("Verdict", "reviewed_at"),
        ("Alert", "created_at"),
        ("Entry", "occurred_at"),
        ("Source", "captured_at"),
        ("Thread", "occurred_at"),
    ],
)
def test_all_dated_models_require_explicit_timezone(model_name, field):
    model = next(m for m in SHARED_MODELS if m.__name__ == model_name)
    payload = example(model_name)
    payload[field] = "2026-11-07T12:00:00"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_request_fields_are_kind_dependent_and_resolution_is_human_only():
    from weaver.schema import Entry

    request = example("Entry")
    decision = example("Entry-decision")
    for broken in [
        request | {"request": None},
        request | {"resolves": decision["entry_id"]},
        decision | {"actor": "ai_approved"},
        decision | {"request": request["request"]},
    ]:
        with pytest.raises(ValidationError):
            Entry.model_validate(broken)


@pytest.mark.parametrize("kind", ["Entity", "Proposal", "Entry"])
def test_confidence_and_nullability(kind):
    model = next(m for m in SHARED_MODELS if m.__name__ == kind)
    payload = example(kind)
    if kind == "Entity":
        payload["confidence"] = 1.01
    elif kind == "Proposal":
        payload["entry_drafts"][0]["occurred_at"] = "2026-11-07T00:00:00"
    else:
        payload["request"]["due_at"] = "2026-11-07T00:00:00"
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("verdict", ["approve", "edit", "revise", "reject"])
@pytest.mark.parametrize("edits", [[], [{"path": "/summary", "value": "수정"}]])
@pytest.mark.parametrize("reason", [None, "wrong_content"])
@pytest.mark.parametrize("instruction", [None, "다시 작성"])
def test_all_verdict_input_combinations(verdict, edits, reason, instruction):
    payload = example("Verdict") | dict(
        verdict=verdict, edits=edits, reject_reason=reason, instruction=instruction
    )
    valid = (
        bool(edits) == (verdict == "edit")
        and (reason is not None) == (verdict == "reject")
        and (instruction is not None) == (verdict == "revise")
    )
    if valid:
        Verdict.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            Verdict.model_validate(payload)


@pytest.mark.parametrize("path", ["", "/summary", "/schedules/0/start", "/a~1b/m~0n"])
def test_valid_json_pointers(path):
    payload = example("Verdict") | dict(
        verdict="edit", edits=[{"path": path, "value": None}]
    )
    assert Verdict.model_validate(payload).edits[0].path == path


@pytest.mark.parametrize("kind", ["text", "kakao", "audio"])
def test_source_thread_pair_preserves_metadata(kind):
    from weaver.schema import Source

    source = Source.model_validate(example(f"Source-{kind}"))
    thread = Thread.model_validate(example(f"Thread-{kind}"))
    assert (source.source_id, source.channel, source.occurred_at) == (
        thread.source_id,
        thread.channel,
        thread.occurred_at,
    )
    if kind == "kakao":
        assert all(
            segment.speaker and segment.occurred_at for segment in thread.segments
        )
    elif kind == "audio":
        assert all(
            segment.start_ms is not None and segment.end_ms is not None
            for segment in thread.segments
        )
    else:
        assert all(
            segment.start_ms is None and segment.end_ms is None
            for segment in thread.segments
        )
