"""Diagnostics must not leak input via values, keys, ctx or validator messages."""

import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from weaver.failures import failure_reason

SECRET = "synthetic-private-source"


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: str

    @model_validator(mode="after")
    def reject(self):
        raise ValueError(SECRET)


@pytest.mark.parametrize(
    "data", [{"direction": 123, SECRET: SECRET}, {"direction": SECRET}]
)
def test_validation_diagnostics_redact_all_user_controlled_parts(data):
    with pytest.raises(ValidationError) as caught:
        Payload.model_validate(data)
    result = failure_reason(caught.value)
    assert SECRET not in result
    assert "input_value" not in result and '"ctx"' not in result
    details = json.loads(result)
    assert all(set(item) == {"type", "loc", "msg"} for item in details)
    assert len(details) <= 5


def test_plain_exception_messages_are_never_logged():
    assert SECRET not in failure_reason(ValueError(SECRET))
    assert "correction_provenance" in failure_reason(
        ValueError("correction was not selected or observed")
    )


@pytest.mark.parametrize(
    "message,reason",
    [
        ("invalid schedule time", "schedule_time_format"),
        ("unknown timezone", "schedule_timezone"),
        (
            "blocked_by_index must reference a request in this batch",
            "request_reference",
        ),
    ],
)
def test_semantic_failure_reasons(message, reason):
    assert json.loads(failure_reason(ValueError(message))) == {"msg": reason}
