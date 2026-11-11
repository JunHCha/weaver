"""Approval transport values retain the field-specific ADR-033 shapes."""

import pytest
from pydantic import TypeAdapter, ValidationError

from weaver.loom.summary_review import ExtractedThread


def extract(field, value):
    return TypeAdapter(ExtractedThread).validate_python(
        dict(
            kind="기록",
            content="synthetic fact",
            occurred_at="2026-01-01T00:00:00Z",
            tags=[],
            field=field,
            value=value,
            direction=None,
            resolves=None,
            retracts=None,
        )
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schedule", {"start": "2026-01-01T09:00:00", "end": None, "timezone": None}),
        ("schedule", {"start": "2026-01-01T09:00:00"}),
        ("assignees", {"add": ["synthetic"], "remove": []}),
        ("deadline", "2026-01-02"),
        ("status", "done"),
        ("assignees", None),
        ("schedule", None),
        (None, None),
    ],
)
def test_supported_shapes(field, value):
    assert extract(field, value).model_dump()["value"] == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schedule", "tomorrow"),
        ("schedule", {"add": [], "remove": []}),
        ("assignees", ["synthetic"]),
        ("assignees", {"add": [1], "remove": []}),
        ("assignees", {"start": "2026-01-01"}),
        ("status", {"add": [], "remove": []}),
        ("deadline", 10),
        (None, "value"),
    ],
)
def test_invalid_field_values(field, value):
    with pytest.raises(ValidationError):
        extract(field, value)
