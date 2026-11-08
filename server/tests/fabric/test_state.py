"""Executable rule table for issue #6; all facts live in temporary SQLite DBs."""

from pathlib import Path

import pytest

from weaver.fabric import EntryQuery, FabricStore
from weaver.schema import EntityState

from .test_store import entry

ENTITY = entry().entities[0]


@pytest.mark.parametrize(
    "field", ["deadline", "date", "due_at", "status", "assignee", "custom"]
)
def test_single_value_and_null_keep_evidence(tmp_path: Path, field: str) -> None:
    store = FabricStore(tmp_path / "facts.db")
    old = entry(1, field=field, value="before")
    new = entry(2, field=field, value=None, source_id="output:edited")
    store.append([new, old])
    state = store.state(ENTITY)
    assert state.fields[field].value is None
    assert state.fields[field].entry_id == new.entry_id
    assert state.fields[field].source_id == new.source_id
    assert len(store.entries(EntryQuery())) == 2


def test_accumulation_keeps_distinct_ids_and_kind_precedence(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    items = [
        entry(1, field=None, value="same"),
        entry(2, field=None, value="same"),
        entry(3, kind="결정", value="same"),
        entry(4, kind="논의", field="assignees", value="same"),
    ]
    store.append(items)
    state = store.state(ENTITY)
    assert state.accumulated == items
    assert state.fields == {}
    assert state.sets == {}


def test_set_operations_json_types_and_last_add_evidence(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    items = [
        entry(
            1,
            field="assignees",
            value={"add": [True, 1, "1", {"x": [False]}, {"x": [0]}], "remove": []},
        ),
        entry(
            2,
            field="assignees",
            value={"add": [1.0, {"x": [False]}], "remove": [True, "1"]},
        ),
    ]
    store.append(items)
    members = store.state(ENTITY).sets["assignees"].members
    assert [m.value for m in members] == [1.0, {"x": [False]}, {"x": [0]}]
    assert [m.entry_id for m in members] == [
        items[1].entry_id,
        items[1].entry_id,
        items[0].entry_id,
    ]
    assert all(m.source_id == items[0].source_id for m in members)
    clear = entry(3, field="assignees", value=None)
    store.append([clear])
    result = store.state(ENTITY).sets["assignees"]
    assert result.members == []
    assert result.entry_id == clear.entry_id


@pytest.mark.parametrize(
    "value",
    [
        [],
        "person",
        {},
        {"add": "person", "remove": []},
        {"add": [], "remove": [], "other": []},
        {"add": []},
    ],
)
def test_invalid_set_operation_is_explicit(tmp_path: Path, value: object) -> None:
    from weaver.fabric import InvalidSetOperation

    store = FabricStore(tmp_path / "facts.db")
    item = entry(field="assignees", value=value)
    store.append([item])
    with pytest.raises(InvalidSetOperation) as error:
        store.state(ENTITY)
    assert error.value.entry_id == item.entry_id
    assert error.value.field == "assignees"


def test_reference_graph_beats_event_clock_and_restores_chain(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    a = entry(1, value="a")
    b = entry(2, value="b", supersedes=a.entry_id, occurred_at="2025-01-01T00:00:00Z")
    c = entry(3, value="c", supersedes=b.entry_id, occurred_at="2024-01-01T00:00:00Z")
    store.append([a, c, b])
    assert store.state(ENTITY).fields["deadline"].value == "c"
    # Superseded b still suppresses a; only retraction cancels its effect.
    r = entry(4, retracts=c.entry_id, value="ignored")
    store.append([r])
    assert store.state(ENTITY).fields["deadline"].value == "b"
    r2 = entry(5, retracts=b.entry_id, value="ignored")
    store.append([r2])
    assert store.state(ENTITY).fields["deadline"].value == "a"


def test_branches_only_active_replacements_suppress(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    a = entry(1, value="a")
    b = entry(2, value="b", supersedes=a.entry_id)
    c = entry(3, value="c", supersedes=a.entry_id)
    store.append([a, b, c, entry(4, retracts=c.entry_id)])
    assert store.state(ENTITY).fields["deadline"].value == "b"
    assert store.state(ENTITY).accumulated == []


def request(n: int, **changes: object):
    return entry(
        n,
        kind="요청",
        field=None,
        request={
            "requester": ENTITY,
            "targets": [ENTITY],
            "due_at": None,
            "content": "approve?",
        },
        **changes,
    )


def decision(n: int, target: str, **changes: object):
    return entry(n, kind="결정", field=None, actor="human", resolves=target, **changes)


def test_requests_direct_resolution_latest_decision_and_history(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    a = request(1)
    b = decision(2, a.entry_id, value="yes")
    c = decision(3, a.entry_id, value="no")
    store.append([a, b, c])
    state = store.state(ENTITY)
    assert state.open_requests == []
    assert state.decisions == {a.entry_id: c}
    assert state.accumulated == [a, b, c]
    r = entry(4, field=None, retracts=c.entry_id)
    store.append([r])
    assert store.state(ENTITY).decisions == {a.entry_id: b}
    store.append([entry(5, field=None, retracts=b.entry_id)])
    assert store.state(ENTITY).open_requests == [a]
    assert store.state(ENTITY).decisions == {}


def test_replacing_request_does_not_inherit_decision(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    a = request(1)
    b = request(3, supersedes=a.entry_id)
    d = decision(2, a.entry_id)
    store.append([b, d, a])
    assert store.state(ENTITY).open_requests == [b]
    assert store.state(ENTITY).decisions == {a.entry_id: d}


def test_replay_restart_ordering_entity_scope_and_no_cached_mutation(
    tmp_path: Path,
) -> None:
    from weaver.fabric import EntryQuery

    store = FabricStore(tmp_path / "facts.db")
    items = [
        entry(4, value="id-winner"),
        entry(3, value="timezone", occurred_at="2026-01-01T09:00:00+09:00"),
        entry(8, value="earlier-recorded", recorded_at="2026-01-01T00:00:00Z"),
        entry(
            9,
            value="late-old",
            occurred_at="2025-01-01T00:00:00Z",
            recorded_at="2027-01-01T00:00:00Z",
        ),
        entry(10, entities=[f"ent_{3:026d}"], value="other"),
    ]
    store.append(items[:3])
    store.append(items[3:])
    expected = store.state(ENTITY)
    assert expected.fields["deadline"].value == "id-winner"
    assert store.state(items[0].entities[1]) == expected.model_copy(
        update={"entity_id": items[0].entities[1]}
    )
    assert store.state(f"ent_{4:026d}") == EntityState(entity_id=f"ent_{4:026d}")
    assert store.state(ENTITY) == FabricStore(store.path).state(ENTITY)
    replay = FabricStore(tmp_path / "replay.db")
    replay.append(list(reversed(store.entries(EntryQuery()))))
    assert replay.state(ENTITY).model_dump_json() == expected.model_dump_json()
    expected.fields.clear()
    assert store.state(ENTITY).fields


def test_retraction_of_retraction_remains_rejected(tmp_path: Path) -> None:
    from weaver.fabric import InvalidReference

    store = FabricStore(tmp_path / "facts.db")
    a = entry(1)
    r = entry(2, retracts=a.entry_id)
    store.append([a, r])
    with pytest.raises(InvalidReference, match="retraction_of_retraction"):
        store.append([entry(3, retracts=r.entry_id)])
    assert store.state(ENTITY).fields == {}


@pytest.mark.parametrize("clock", ["occurred_at", "recorded_at"])
@pytest.mark.parametrize("value", [None, "2026-01-01T00:00:00"])
def test_unknown_or_naive_time_is_not_guessed(clock: str, value: object) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        entry(**{clock: value})


def test_new_process_replays_same_state(tmp_path: Path) -> None:
    import subprocess
    import sys

    store = FabricStore(tmp_path / "facts.db")
    store.append([entry(1), entry(2, field=None, kind="결정")])
    code = (
        "from pathlib import Path; import sys; "
        "from weaver.fabric import FabricStore; "
        "print(FabricStore(Path(sys.argv[1])).state(sys.argv[2]).model_dump_json())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.path), ENTITY],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == store.state(ENTITY).model_dump_json()


def test_long_reference_dag_has_no_recursion_limit(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    items = [entry(1, field=None)]
    for n in range(2, 1501):
        items.append(entry(n, field=None, supersedes=items[-1].entry_id))
    store.append(list(reversed(items)))
    assert store.state(ENTITY).accumulated == [items[-1]]


def test_ai_approval_and_cross_entity_block_do_not_resolve_request(
    tmp_path: Path,
) -> None:
    store = FabricStore(tmp_path / "facts.db")
    a = request(1)
    b = entry(2, kind="결정", field=None)
    c = entry(3, entities=[f"ent_{3:026d}"], blocked_by_request=a.entry_id)
    store.append([c, b, a])
    state = store.state(ENTITY)
    assert state.open_requests == [a]
    assert state.decisions == {}
    assert state.accumulated == [a, b]


def test_set_remove_add_order_and_restoration(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    a = entry(1, field="assignees", value={"add": ["a", "a"], "remove": ["absent"]})
    b = entry(
        2,
        field="assignees",
        value={"add": ["b"], "remove": ["b"]},
        supersedes=a.entry_id,
    )
    store.append([a, b])
    assert store.state(ENTITY).sets["assignees"].members == []
    store.append([entry(3, field="assignees", retracts=b.entry_id)])
    members = store.state(ENTITY).sets["assignees"].members
    assert len(members) == 1
    assert members[0].value == "a"
    assert members[0].entry_id == a.entry_id


def test_retraction_decision_never_resolves_or_accumulates(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    a = request(1)
    b = entry(2, field=None, value="note")
    r = decision(3, a.entry_id, retracts=b.entry_id)
    store.append([a, b, r])
    state = store.state(ENTITY)
    assert state.open_requests == [a]
    assert state.decisions == {}
    assert state.accumulated == [a]


@pytest.mark.parametrize("field", [None, "assignees"])
def test_replacement_chain_does_not_restore_original_contribution(
    tmp_path: Path,
    field: str | None,
) -> None:
    store = FabricStore(tmp_path / "facts.db")
    values = (
        ["draft", "fix1", "fix2"]
        if field is None
        else [{"add": [person], "remove": []} for person in ["kim", "lee", "park"]]
    )
    a = entry(1, field=field, value=values[0])
    b = entry(2, field=field, value=values[1], supersedes=a.entry_id)
    c = entry(3, field=field, value=values[2], supersedes=b.entry_id)
    store.append([c, a, b])
    state = store.state(ENTITY)
    if field is None:
        assert state.accumulated == [c]
    else:
        assert [member.value for member in state.sets[field].members] == ["park"]


def test_request_replacement_chain_only_latest_is_open(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    a = request(1)
    b = request(2, supersedes=a.entry_id)
    c = request(3, supersedes=b.entry_id)
    store.append([c, a, b])
    assert store.state(ENTITY).open_requests == [c]
    assert store.state(ENTITY).accumulated == [c]


def test_decision_replacement_chain_and_representative(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "facts.db")
    q = request(1)
    # Reverse event time proves reference suppression precedes clock ordering.
    a = decision(2, q.entry_id, value="draft")
    b = decision(
        3,
        q.entry_id,
        value="fix1",
        supersedes=a.entry_id,
        occurred_at="2025-01-01T00:00:00Z",
    )
    c = decision(
        4,
        q.entry_id,
        value="fix2",
        supersedes=b.entry_id,
        occurred_at="2024-01-01T00:00:00Z",
    )
    store.append([q, a, c, b])
    state = store.state(ENTITY)
    assert state.decisions == {q.entry_id: c}
    assert state.accumulated == [c, q]
    assert state.open_requests == []
    store.append([entry(5, field=None, retracts=c.entry_id)])
    assert store.state(ENTITY).decisions == {q.entry_id: b}
    store.append([entry(6, field=None, retracts=b.entry_id)])
    assert store.state(ENTITY).decisions == {q.entry_id: a}
    assert len(store.entries(EntryQuery())) == 6


@pytest.mark.parametrize("add_first", [True, False])
def test_set_add_remove_timestamp_tie_uses_entry_id(
    tmp_path: Path,
    add_first: bool,
) -> None:
    store = FabricStore(tmp_path / "facts.db")
    add = entry(
        1 if add_first else 2, field="assignees", value={"add": ["kim"], "remove": []}
    )
    remove = entry(
        2 if add_first else 1, field="assignees", value={"add": [], "remove": ["kim"]}
    )
    store.append([add, remove])
    members = store.state(ENTITY).sets["assignees"].members
    assert [member.value for member in members] == ([] if add_first else ["kim"])
    if members:
        assert members[0].entry_id == add.entry_id


@pytest.mark.parametrize("same_batch", [True, False])
def test_superseding_retraction_is_rejected_atomically(
    tmp_path: Path,
    same_batch: bool,
) -> None:
    from weaver.fabric import InvalidReference

    store = FabricStore(tmp_path / "facts.db")
    a = entry(1)
    r = entry(2, retracts=a.entry_id)
    replacement = entry(3, supersedes=r.entry_id)
    if not same_batch:
        store.append([a, r])
    with pytest.raises(InvalidReference) as error:
        store.append([replacement, r, a] if same_batch else [replacement])
    assert error.value.entry_id == replacement.entry_id
    assert error.value.field == "supersedes"
    assert error.value.reason == "supersede_of_retraction"
    assert store.entries(EntryQuery()) == ([] if same_batch else [a, r])
    assert store.state(ENTITY).fields == {}
