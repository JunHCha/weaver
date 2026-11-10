"""Acceptance tests for the v2 append-only thread log."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from weaver.fabric import FabricStore, ThreadConflict, ThreadQuery
from weaver.schema.v2 import ThreadDraft


def draft(key: str, **changes: object) -> ThreadDraft:
    data = dict(
        thread_id=key,
        kind="기록",
        content="기록",
        occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        recorded_at=datetime(2025, 1, 2, tzinfo=UTC),
        tags=["홍길동"],
        field=None,
        value=None,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        actor="human",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )
    data.update(changes)
    return ThreadDraft.model_validate(data)


def test_sequence_idempotency_conflict_and_atomic_batch(tmp_path):
    store = FabricStore(tmp_path / "fabric.sqlite3")
    a = draft("a")
    assert store.append([a])[0].seq == 1
    assert store.append([a])[0].seq == 1
    with pytest.raises(ThreadConflict):
        store.append([draft("b"), draft("a", content="다름")])
    assert [t.thread_id for t in store.entries(ThreadQuery())] == ["a"]
    assert store.append([draft("b")])[0].seq == 2


def test_concurrent_appends_assign_unique_monotonic_sequences(tmp_path):
    path = tmp_path / "fabric.sqlite3"
    FabricStore(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = list(
            pool.map(
                lambda n: FabricStore(path).append([draft(str(n))])[0].seq, range(30)
            )
        )
    assert sorted(seqs) == list(range(1, 31))


def test_late_arrival_is_sorted_by_occurrence_but_found_by_sequence(tmp_path):
    store = FabricStore(tmp_path / "fabric.sqlite3")
    now = datetime(2025, 1, 2, tzinfo=UTC)
    store.append([draft("new", occurred_at=now)])
    store.append([draft("old", occurred_at=now - timedelta(days=1))])
    assert [t.thread_id for t in store.entries(ThreadQuery())] == ["old", "new"]
    assert [t.thread_id for t in store.entries(ThreadQuery(after_seq=1))] == ["old"]


def test_invalid_references_rollback_and_valid_batch(tmp_path):
    from weaver.fabric import InvalidReference

    store = FabricStore(tmp_path / "db")
    with pytest.raises(InvalidReference):
        store.append([draft("a"), draft("b", retracts="missing")])
    assert store.entries(ThreadQuery()) == []
    store.append([draft("q", kind="요청", direction="받음"), draft("d", kind="결정", resolves="q")])
    for bad in [draft("x", kind="결정", resolves="d"), draft("x", retracts="missing"), draft("x", kind="태그정정", correction={"target": "missing", "add": [], "remove": []})]:
        with pytest.raises(InvalidReference):
            store.append([bad])
    store.append([draft("r", retracts="d")])
    with pytest.raises(InvalidReference):
        store.append([draft("rr", retracts="r")])


def test_effective_tags_and_retracted_correction(tmp_path):
    store = FabricStore(tmp_path / "db")
    store.append([draft("a", tags=["A"]), draft("b", kind="태그정정", correction={"target": "a", "remove": ["A"], "add": ["B"]}), draft("c", kind="태그정정", correction={"target": "a", "remove": ["B"], "add": ["C"]})])
    assert [t.thread_id for t in store.entries(ThreadQuery(tag="C"))] == ["a"]
    store.append([draft("r", retracts="c")])
    assert [t.thread_id for t in store.entries(ThreadQuery(tag="B"))] == ["a"]
    assert store.entries(ThreadQuery(tag="C")) == []


def test_state_fields_assignees_and_open_requests(tmp_path):
    store = FabricStore(tmp_path / "db")
    store.append([draft("q1", kind="요청", direction="받음"), draft("q2", kind="요청", direction="보냄"), draft("d", kind="결정", resolves="q1"), draft("a", field="status", value="old"), draft("b", field="status", value="new"), draft("s1", field="assignees", value={"add": ["A", "B"], "remove": []}), draft("s2", field="assignees", value={"add": ["C"], "remove": ["A"]})])
    state = store.state("홍길동")
    assert state.fields["status"].value == "new"
    assert state.fields["status"].thread_id == "b"
    assert state.assignees == {"B", "C"}
    assert [(t.thread_id, t.direction) for t in state.open_requests] == [("q2", "보냄")]
    store.append([draft("r", retracts="d")])
    assert [t.thread_id for t in store.state("홍길동").open_requests] == ["q1", "q2"]


def test_contact_statistics_and_old_version(tmp_path):
    import sqlite3

    from weaver.fabric import SchemaVersionError

    path = tmp_path / "old"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=1")
    with pytest.raises(SchemaVersionError):
        FabricStore(path)
    store = FabricStore(tmp_path / "new")
    store.append([draft("a"), draft("b", occurred_at=datetime(2025, 1, 3, tzinfo=UTC))])
    assert store.contact_statistics("홍길동", since=datetime(2025, 1, 2, tzinfo=UTC)).count == 1
    assert store.contact_statistics("홍길동").latest == datetime(2025, 1, 3, tzinfo=UTC)
