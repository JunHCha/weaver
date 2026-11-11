"""Bounded diagnostics without source text, arbitrary keys or exception reprs."""

import json

from pydantic import ValidationError

# Error messages and locations can themselves contain user data (custom
# validators, dict keys, extra fields). Only static vocabulary reaches logs.
_FIELDS = frozenset(
    "body corrections threads observed canonical kind content occurred_at tags "
    "field value direction resolves retracts blocked_by_index start end timezone "
    "add remove thread_id recorded_at source_id actor correction blocked_by".split()
)
_MESSAGES = {
    "missing": "Field required",
    "value_error": "Validation rule failed",
    "literal_error": "Unexpected literal",
    "none_required": "Input should be None",
    "string_type": "Input should be a string",
    "string_pattern_mismatch": "String does not match required pattern",
    "int_type": "Input should be an integer",
    "int_parsing": "Input should be an integer",
    "greater_than_equal": "Number is below minimum",
    "extra_forbidden": "Extra inputs are not permitted",
    "dict_type": "Input should be a dictionary",
    "list_type": "Input should be a list",
    "datetime_parsing": "Invalid datetime",
    "datetime_from_date_parsing": "Invalid datetime",
    "timezone_aware": "Input should have timezone info",
}
_RULES = {
    "end must follow start": "schedule_end_order",
    "invalid schedule time": "schedule_time_format",
    "schedule value must be an object": "schedule_shape",
    "schedule timezone must be a string": "schedule_timezone_type",
    "schedule time must be a string": "schedule_time_type",
    "unknown timezone": "schedule_timezone",
    "expected RFC 3339 timestamp with an explicit timezone": "timestamp_format",
    "direction is required exactly for requests": "request_direction",
    "blocked_by_index must reference a request in this batch": "request_reference",
    "correction was not selected or observed": "correction_provenance",
    "nonexistent local schedule time": "schedule_local_time",
}


def failure_reason(error: Exception) -> str:
    """Return sanitized type/loc/msg only; never include input, ctx or URLs."""
    if not isinstance(error, ValidationError):
        # Equality against fixed messages is safe; arbitrary messages are never
        # returned, even for subclasses supplied by clients or storage adapters.
        reason = _RULES.get(str(error), "operation_failed")
        return json.dumps({"msg": reason})
    details = []
    for item in error.errors(
        include_input=False, include_context=False, include_url=False
    ):
        kind = item["type"] if item["type"] in _MESSAGES else "validation_error"
        location = [
            part if isinstance(part, int) or part in _FIELDS else "<field>"
            for part in item["loc"]
        ]
        message = _RULES.get(
            item["msg"].removeprefix("Value error, "),
            _MESSAGES.get(kind, "validation rule failed"),
        )
        detail = {"type": kind, "loc": location, "msg": message}
        if detail not in details:
            details.append(detail)
        if len(details) == 5:
            break
    return json.dumps(details, ensure_ascii=False)
