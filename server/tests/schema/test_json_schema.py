"""Consumer parity for structural rules; cross-value invariants stay server-side."""

from itertools import product

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from weaver.schema import Entity, Manifest, Proposal, Thread, Verdict
from weaver.schema.models import ProposalEntity, Segment, ThreadDraft

from .test_contract import example


def verdicts(model, payload):
    schema = model.model_json_schema(mode="validation", by_alias=True)
    Draft202012Validator.check_schema(schema)
    json_valid = Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(
        payload
    )
    try:
        model.model_validate(payload)
    except ValidationError:
        python_valid = False
    else:
        python_valid = True
    return python_valid, json_valid


def agree(model, payload, expected):
    assert verdicts(model, payload) == (expected, expected)


@pytest.mark.parametrize(
    ("verdict", "has_edits", "reason", "instruction"),
    list(
        product(
            ["approve", "edit", "revise", "reject"],
            [False, True],
            [None, "wrong_person"],
            [None, "다시 확인"],
        )
    ),
)
def test_verdict_32_branches(verdict, has_edits, reason, instruction):
    payload = example("Verdict") | dict(
        verdict=verdict,
        edits=[{"path": "/summary", "value": "새 요약"}] if has_edits else [],
        reject_reason=reason,
        instruction=instruction,
    )
    agree(
        Verdict,
        payload,
        has_edits == (verdict == "edit")
        and (reason is not None) == (verdict == "reject")
        and (instruction is not None) == (verdict == "revise"),
    )


@pytest.mark.parametrize(
    ("kind", "has_request", "has_resolves", "actor"),
    list(
        product(
            ["요청", "논의", "결정", "폐기", "약속", "일정", "변경"],
            [False, True],
            [False, True],
            ["human", "ai_approved"],
        )
    ),
)
def test_entry_kind_request_resolution_actor(kind, has_request, has_resolves, actor):
    original = example("Thread")
    payload = original | dict(
        kind=kind,
        request=original["request"] if has_request else None,
        resolves=original["entry_id"] if has_resolves else None,
        actor=actor,
    )
    agree(
        Thread,
        payload,
        has_request == (kind == "요청")
        and (not has_resolves or (kind == "결정" and actor == "human")),
    )


@pytest.mark.parametrize(("has_ref", "has_id"), list(product([False, True], repeat=2)))
def test_entity_identity(has_ref, has_id):
    payload = example("Entity") | dict(
        ref="e1" if has_ref else None,
        entity_id=example("Thread")["entities"][0] if has_id else None,
    )
    agree(Entity, payload, has_ref or has_id)
    del payload["schema_version"]
    agree(ProposalEntity, payload, has_ref)


@pytest.mark.parametrize("ref", [None, "e1"])
def test_full_proposal_requires_nonnull_entity_ref(ref):
    payload = example("Proposal")
    payload["entities"][0]["ref"] = ref
    payload["entities"][0]["entity_id"] = example("Thread")["entities"][0]
    agree(Proposal, payload, ref is not None)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (None, None, True),
        (0, 100, True),
        (None, 100, False),
        (0, None, True),
    ],
)
def test_segment_audio_offset_pairs(start, end, expected):
    payload = example("Script")["segments"][0] | dict(start_ms=start, end_ms=end)
    agree(Segment, payload, expected)


@pytest.mark.parametrize("resolves", [None, "d1", "evt_01ARZ3NDEKTSV4RRFFQ69G5FA0"])
def test_ai_draft_resolves_is_null_only(resolves):
    payload = example("Proposal")["entry_drafts"][1] | dict(
        kind="결정", resolves=resolves
    )
    agree(ThreadDraft, payload, resolves is None)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-07T15:32:00+09:00",
        "2026-11-07T06:32:00Z",
        "2026-11-07t06:32:00Z",
        "2026-11-07T06:32:00z",
        "2026-11-07T06:32:00",
        "2026-11-07T06:32:00Z\n",
        "2026-02-30T06:32:00Z",
        "2026-11-07T06:32:00+24:00",
    ],
)
def test_date_time_pattern_and_format(timestamp):
    agree(
        Manifest,
        example("Manifest") | {"captured_at": timestamp},
        timestamp in ["2026-11-07T15:32:00+09:00", "2026-11-07T06:32:00Z"],
    )


@pytest.mark.parametrize("suffix", ["", "\n", "\r\n", " "])
def test_ulid_has_exact_length_in_both_validators(suffix):
    payload = example("Manifest")
    payload["capture_id"] += suffix
    agree(Manifest, payload, not suffix)


@pytest.mark.parametrize(
    ("model", "field"), [(Manifest, "schema_version"), (Verdict, "rev")]
)
def test_integer_lexical_rule_cannot_be_expressed_in_json_schema(model, field):
    # Draft 2020-12 integer is mathematical, so 1 and 1.0 are indistinguishable.
    # Consumers must also enforce the documented integer-token rule when decoding.
    payload = example(model.__name__) | {field: 1.0}
    assert verdicts(model, payload) == (False, True)


def test_all_exported_schema_examples_validate_independently():
    import json

    from .test_contract import ROOT

    validators = {}
    for path in (ROOT / "schema").glob("*.schema.json"):
        schema = json.loads(path.read_text())
        Draft202012Validator.check_schema(schema)
        validators[path.name.split(".")[0]] = Draft202012Validator(
            schema, format_checker=FormatChecker()
        )
    for path in (ROOT / "schema" / "examples").glob("*.json"):
        validators[path.stem.split("-")[0]].validate(json.loads(path.read_text()))


@pytest.mark.parametrize(
    "name,field",
    [
        ("Manifest", "capture_id"),
        ("Receipt", "source_id"),
        ("Proposal", "proposal_id"),
        ("Alert", "alert_id"),
        ("Entity", "entity_id"),
        ("Thread", "entry_id"),
        ("Entity", "ref"),
        ("Manifest", "sha256"),
        ("Manifest", "payload"),
        ("Thread", "source_id"),
    ],
)
def test_identifier_and_path_trailing_newline_is_rejected(name, field):
    from weaver.schema import SHARED_MODELS

    model = next(m for m in SHARED_MODELS if m.__name__ == name)
    payload = example(name)
    if name == "Entity" and field == "entity_id":
        payload[field] = example("Thread")["entities"][0]
    if name == "Thread" and field == "source_id":
        payload[field] = "output:review/request"
    payload[field] += "\n"
    agree(model, payload, False)


def test_proposal_nested_items_do_not_repeat_version_keys():
    payload = example("Proposal")
    for collection in ["entities", "relations", "schedules", "entry_drafts"]:
        assert all("schema_version" not in item for item in payload[collection])
        modified = example("Proposal")
        modified[collection][0]["schema_version"] = 1
        agree(Proposal, modified, False)


@pytest.mark.parametrize(
    "start,end,expected",
    [(None, None, True), (0, None, True), (0, 100, True), (None, 100, False)],
)
def test_exported_thread_allows_unknown_end_with_consumer_parity(start, end, expected):
    import json

    from weaver.schema import Script

    from .test_contract import ROOT

    payload = example("Script")
    payload["segments"][0].update(start_ms=start, end_ms=end)
    schema = json.loads((ROOT / "schema/Script.schema.json").read_text())
    assert (
        Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(payload)
        == expected
    )
    agree(Script, payload, expected)
