"""Consumer parity for structural rules; cross-value invariants stay server-side."""

import json
from itertools import product

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from weaver.schema import Manifest, Thread, ThreadDraft, Verdict
from weaver.schema.models import Segment

from .test_contract import ROOT, example, model_named


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
    ("action", "edited", "feedback"),
    list(
        product(
            ["approve", "edit", "feedback", "reject"],
            [None, "수정 본문"],
            [None, "다시 확인"],
        )
    ),
)
def test_verdict_branches(action, edited, feedback):
    payload = example("Verdict") | dict(
        action=action, edited_body=edited, feedback=feedback
    )
    agree(
        Verdict,
        payload,
        (action != "edit" or edited is not None)
        and (action not in {"approve", "reject"} or edited is None)
        and (action != "feedback")
        and (action != "reject" or feedback is None),
    )


@pytest.mark.parametrize(
    ("kind", "direction", "correction", "content"),
    list(
        product(
            ["요청", "결정", "일정", "기록", "태그정정", "거절"],
            [None, "받음"],
            [None, {"target": "thread-2", "remove": [], "add": ["새"]}],
            [None, "내용"],
        )
    ),
)
def test_thread_kind_dependent_fields(kind, direction, correction, content):
    payload = example("Thread") | dict(
        kind=kind,
        direction=direction,
        correction=correction,
        content=content,
        field=None,
        value=None,
    )
    agree(
        Thread,
        payload,
        (direction is not None) == (kind == "요청")
        and (correction is not None) == (kind == "태그정정")
        and (content is None) == (kind == "거절"),
    )


@pytest.mark.parametrize("resolves", [None, "thread-2"])
@pytest.mark.parametrize("kind", ["요청", "결정", "기록"])
def test_only_decisions_resolve_requests(kind, resolves):
    payload = example("Thread") | dict(
        kind=kind,
        direction="받음" if kind == "요청" else None,
        resolves=resolves,
        field=None,
        value=None,
    )
    agree(Thread, payload, resolves is None or kind == "결정")


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(None, None, True), (0, 100, True), (None, 100, False), (0, None, True)],
)
def test_segment_audio_offset_pairs(start, end, expected):
    payload = example("Script")["segments"][0] | dict(start_ms=start, end_ms=end)
    agree(Segment, payload, expected)


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
    ("model", "field"), [(Manifest, "schema_version"), (Verdict, "revision")]
)
def test_integer_lexical_rule_cannot_be_expressed_in_json_schema(model, field):
    # Draft 2020-12 integer is mathematical, so 1 and 1.0 are indistinguishable.
    # Consumers must also enforce the documented integer-token rule when decoding.
    payload = example(model.__name__) | {
        field: 1.0 if field != "schema_version" else 1.0
    }
    assert verdicts(model, payload) == (False, True)


def test_all_exported_schema_examples_validate_independently():
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
        ("Alert", "alert_id"),
        ("Manifest", "sha256"),
        ("Manifest", "payload"),
        ("Thread", "source_id"),
        ("EventSummary", "summary_id"),
    ],
)
def test_identifier_and_path_trailing_newline_is_rejected(name, field):
    payload = example(name)
    payload[field] += "\n"
    agree(model_named(name), payload, False)


@pytest.mark.parametrize(
    "start,end,expected",
    [(None, None, True), (0, None, True), (0, 100, True), (None, 100, False)],
)
def test_exported_script_allows_unknown_end_with_consumer_parity(start, end, expected):
    from weaver.schema import Script

    payload = example("Script")
    payload["segments"][0].update(start_ms=start, end_ms=end)
    schema = json.loads((ROOT / "schema/Script.schema.json").read_text())
    assert (
        Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(payload)
        == expected
    )
    agree(Script, payload, expected)


def test_schedule_value_has_consumer_parity():
    schedule = {
        "start": "2026-01-01T09:00:00+09:00",
        "end": None,
        "timezone": "Asia/Seoul",
    }
    base = example("Thread") | dict(kind="일정", field="schedule")
    agree(
        ThreadDraft,
        {k: v for k, v in (base | {"value": schedule}).items() if k != "seq"},
        True,
    )
