"""Provider output that names a Thread field must carry that field's value shape.

A schedule object, an assignee delta, a date or status string, or no value are
accepted; any other shape is rejected before it can reach a review document.
That shared text is interpreted without losing a character is proved by
``properties/test_source_text_lossless.py`` and the parser tests.
"""

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
        ("schedule", None),
        ("assignees", None),
        (None, None),
    ],
    ids=[
        "schedule_full",
        "schedule_start_only",
        "assignee_delta",
        "deadline_date",
        "status_word",
        "schedule_without_value",
        "assignee_without_value",
        "plain_fact",
    ],
)
def test_provider_accepts_each_field_with_its_own_value_shape(field, value):
    assert extract(field, value).model_dump()["value"] == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schedule", "tomorrow"),
        ("schedule", {"add": [], "remove": []}),
        ("assignees", ["synthetic"]),
        ("assignees", {"add": [1], "remove": []}),
        ("status", {"add": [], "remove": []}),
        ("deadline", 10),
        (None, "value"),
    ],
    ids=[
        "schedule_text",
        "schedule_with_assignee_shape",
        "assignees_list",
        "assignee_not_text",
        "status_object",
        "deadline_number",
        "value_without_field",
    ],
)
def test_provider_value_in_the_wrong_shape_is_rejected(field, value):
    with pytest.raises(ValidationError):
        extract(field, value)
