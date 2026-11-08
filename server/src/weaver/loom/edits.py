"""Replacement-only RFC 6901 edits over an explicit field whitelist."""

import re
from typing import Any

from weaver.schema import Proposal, Verdict

_FIELDS = {
    "entities": {"name", "type", "aliases", "entity_id", "ask"},
    "relations": {"from", "to", "type", "reason", "ask"},
    "entry_drafts": {"kind", "field", "value", "occurred_at", "entities", "ask"},
    "schedules": {"title", "start", "end", "entities"},
}
_NESTED = {"aliases", "entities", "value"}


def _index(token: str, value: list[Any]) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", token) or int(token) >= len(value):
        raise ValueError("edit index does not exist")
    return int(token)


def edit_proposal(proposal: Proposal, verdict: Verdict) -> Proposal:
    data = proposal.model_dump(mode="json", by_alias=True)
    for edit in verdict.edits:
        parts = [
            p.replace("~1", "/").replace("~0", "~") for p in edit.path.split("/")[1:]
        ]
        if parts == ["summary"]:
            data["summary"] = edit.value
            continue
        if (
            len(parts) < 3
            or parts[0] not in _FIELDS
            or parts[2] not in _FIELDS[parts[0]]
        ):
            raise ValueError("immutable edit path")
        group, index, field = parts[:3]
        idx = _index(index, data[group])
        if len(parts) > 3 and field not in _NESTED:
            raise ValueError("nested edit forbidden")
        if field == "ask" and edit.value is not None:
            raise ValueError("ask can only be acknowledged with null")
        original = proposal.model_dump(mode="json", by_alias=True)[group][idx]
        if group == "entities" and field == "entity_id":
            choices = original["ask"]["choices"] if original["ask"] else []
            if edit.value is not None and edit.value not in [
                c["entity_id"] for c in choices
            ]:
                raise ValueError("identity is not an original ask choice")
        if group == "entry_drafts" and field == "kind":
            if (original["kind"] == "요청") != (edit.value == "요청"):
                raise ValueError("request kind is immutable")
        parent: Any = data[group][idx]
        for token in parts[2:-1]:
            if isinstance(parent, list):
                parent = parent[_index(token, parent)]
            elif isinstance(parent, dict) and token in parent:
                parent = parent[token]
            else:
                raise ValueError("edit path does not exist")
        key = parts[-1]
        if isinstance(parent, list):
            parent[_index(key, parent)] = edit.value
        elif isinstance(parent, dict) and key in parent:
            parent[key] = edit.value
        else:
            raise ValueError("edit path does not exist")
    return Proposal.model_validate(data)
