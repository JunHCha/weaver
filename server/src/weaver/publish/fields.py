"""Editable output properties shared by Publisher and OutputWatcher.

Flat properties use the same names as Entry.field. Missing and YAML null mean
an explicit cleared value. Only these properties carry editable facts; generated
metadata (entry status, timestamps) and Markdown prose are never interpreted.
"""

from datetime import date

from pydantic import JsonValue, TypeAdapter

from weaver.schema.models import EntityId

OUTPUT_FIELDS = ("deadline", "assignees", "status", "decision")


def normalize_field(field: str, value: JsonValue) -> JsonValue:
    if value is None:
        return None
    if field == "assignees":
        return [
            v for v in sorted(set(TypeAdapter(list[EntityId]).validate_python(value)))
        ]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected nonempty text or null")
    text = " ".join(value.split())
    if field == "deadline":
        # Date-only ISO contract: no implicit timezones or natural-language dates.
        if date.fromisoformat(text).isoformat() != text:
            raise ValueError("expected ISO date")
    return text
