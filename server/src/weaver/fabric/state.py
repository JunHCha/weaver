"""Deterministic state folding over a validated, entity-filtered Fabric snapshot.

Reference activity is evaluated before chronological folding: only an active
superseding/retracting fact suppresses its target. In particular, retracting a
replacement restores the prior fact. Retraction facts never contribute values.
"""

from pydantic import JsonValue

from weaver.schema import EntityState, Entry, SetState, StateValue


class InvalidSetOperation(ValueError):
    """Malformed active assignees operation; never includes fact content."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self.field = "assignees"
        super().__init__("assignees requires null or exactly add/remove JSON arrays")


def _equal(left: JsonValue, right: JsonValue) -> bool:
    """JSON structural equality: booleans differ from numbers, 1 equals 1.0."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equal(value, right[key]) for key, value in left.items()
        )
    return left == right


def _value(entry: Entry, value: JsonValue) -> StateValue:
    return StateValue(value=value, entry_id=entry.entry_id, source_id=entry.source_id)


def _apply_set(entry: Entry, previous: SetState | None) -> SetState:
    members = list(previous.members) if previous is not None else []
    operation = entry.value
    if operation is None:
        members.clear()
    else:
        if (
            not isinstance(operation, dict)
            or operation.keys() != {"add", "remove"}
            or not isinstance(operation["add"], list)
            or not isinstance(operation["remove"], list)
        ):
            raise InvalidSetOperation(entry.entry_id)
        # Apply add then remove: an explicit removal wins within one operation.
        for value in operation["add"]:
            match = next(
                (i for i, member in enumerate(members) if _equal(member.value, value)),
                None,
            )
            if match is None:
                members.append(_value(entry, value))
            else:
                members[match] = _value(entry, value)
        members = [
            member
            for member in members
            if not any(_equal(member.value, value) for value in operation["remove"])
        ]
    return SetState(members=members, entry_id=entry.entry_id, source_id=entry.source_id)


def fold_state(entity_id: str, entries: list[Entry]) -> EntityState:
    """Fold a store-ordered snapshot; storage has validated the reference DAG.

    Supersedes/retracts edges stay within the same entity set. Request blocking
    edges may cross entities but have no effect on this read-only projection.
    Kahn traversal visits suppressors before targets, independent of timestamps,
    without recursion depth limits. No cache or persistent state is maintained.
    """
    by_id = {item.entry_id: item for item in entries}
    incoming = dict.fromkeys(by_id, 0)
    for item in entries:
        target = item.supersedes or item.retracts
        if target is not None:
            incoming[target] += 1
    ready = [key for key, count in incoming.items() if count == 0]
    suppressed: set[str] = set()
    while ready:
        key = ready.pop()
        item = by_id[key]
        target = item.supersedes or item.retracts
        if target is not None:
            if key not in suppressed:
                suppressed.add(target)
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)

    state = EntityState(entity_id=entity_id)
    requests = []
    for item in entries:
        if item.entry_id in suppressed or item.retracts is not None:
            continue
        if item.kind == "요청":
            requests.append(item)
        if item.kind == "결정" and item.actor == "human" and item.resolves is not None:
            state.decisions[item.resolves] = item
        if item.field is None or item.kind in ("결정", "논의"):
            state.accumulated.append(item)
        elif item.field == "assignees":
            state.sets[item.field] = _apply_set(item, state.sets.get(item.field))
        else:
            state.fields[item.field] = _value(item, item.value)
    state.open_requests = [
        request for request in requests if request.entry_id not in state.decisions
    ]
    return state
