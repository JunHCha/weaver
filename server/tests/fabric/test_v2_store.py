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
