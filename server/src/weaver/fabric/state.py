"""Deterministic per-tag projections of the thread log."""

from dataclasses import dataclass, field

from pydantic import JsonValue

from weaver.schema.v2 import Thread


class InvalidSetOperation(ValueError):
    """An assignees operation was malformed."""


@dataclass(frozen=True)
class StateValue:
    """A field value and the thread that supplied it."""

    value: JsonValue
    thread_id: str


@dataclass
class TagState:
    """Current fields, assignees, evidence and unresolved requests for a tag."""

    tag: str
    fields: dict[str, StateValue] = field(default_factory=dict)
    assignees: set[str] = field(default_factory=set)
    assignees_thread_id: str | None = None
    open_requests: list[Thread] = field(default_factory=list)


def project(tag: str, threads: list[Thread]) -> TagState:
    """Project a complete occurrence-ordered snapshot with effective tags."""
    state = TagState(tag)
    retracted = {t.retracts for t in threads if t.retracts is not None}
    active = [t for t in threads if t.thread_id not in retracted and t.retracts is None and t.kind not in ("태그정정", "거절")]
    resolved = {t.resolves for t in active if t.resolves is not None}
    for t in active:
        if tag not in t.tags:
            continue
        if t.kind == "요청" and t.thread_id not in resolved:
            state.open_requests.append(t)
        if t.field == "assignees":
            value = t.value
            if value is None:
                state.assignees.clear()
            elif isinstance(value, dict) and value.keys() == {"add", "remove"} and all(isinstance(value[k], list) and all(isinstance(x, str) for x in value[k]) for k in ("add", "remove")):
                state.assignees.update(value["add"])
                state.assignees.difference_update(value["remove"])
            else:
                raise InvalidSetOperation(t.thread_id)
            state.assignees_thread_id = t.thread_id
        elif t.field is not None:
            state.fields[t.field] = StateValue(t.value, t.thread_id)
    return state
