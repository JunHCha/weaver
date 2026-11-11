"""Check constraints on the actual schema sent to Anthropic, without an LLM."""

import copy

import anthropic
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from weaver.loom.summary_review import ApprovedResponse


@pytest.fixture
def response():
    return {
        "body": "Approved facts",
        "corrections": [],
        "threads": [
            {
                "kind": "요청",
                "content": "Send materials",
                "occurred_at": "2026-01-01T00:00:00Z",
                "tags": [],
                "field": None,
                "value": None,
                "direction": "받음",
                "resolves": None,
                "retracts": None,
                "blocked_by_index": None,
            }
        ],
    }


@pytest.mark.parametrize("kind", ["요청", "결정", "일정", "기록"])
@pytest.mark.parametrize(
    "field,value",
    [
        (None, None),
        ("deadline", "tomorrow"),
        ("status", "done"),
        ("schedule", {"start": "2026-01-01T09:00:00", "end": None, "timezone": None}),
        ("assignees", {"add": ["reviewer"], "remove": []}),
    ],
)
def test_transformed_schema_keeps_kind_and_field_constraints(
    response, kind, field, value
):
    validator = Draft202012Validator(anthropic.transform_schema(ApprovedResponse))
    thread = response["threads"][0]
    thread.update(
        kind=kind,
        direction="보냄" if kind == "요청" else None,
        field=field,
        value=value,
    )
    validator.validate(response)
    ApprovedResponse.model_validate(response)
    for change in (
        [
            {"direction": None if kind == "요청" else "받음"},
            {"value": {"wrong": "shape"}},
            {"field": None, "value": "orphan value"},
            {"blocked_by_index": -1},
            {"content": None},
            {"kind": "태그정정"},
            {"correction": {"target": "old", "add": [], "remove": []}},
        ]
        + ([{"resolves": "request"}] if kind != "결정" else [])
        + ([{"blocked_by_index": 0}] if kind != "일정" else [])
    ):
        invalid = copy.deepcopy(response)
        invalid["threads"][0].update(change)
        # Numeric minimums are stripped by Anthropic, but enforced locally.
        if change != {"blocked_by_index": -1}:
            assert not validator.is_valid(invalid), change
        with pytest.raises(ValidationError):
            ApprovedResponse.model_validate(invalid)
    if kind == "결정":
        thread["resolves"] = "request"
        validator.validate(response)
    if kind == "일정":
        thread["blocked_by_index"] = 0
        validator.validate(response)
