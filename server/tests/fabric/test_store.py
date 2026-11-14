"""Acceptance tests for the v2 append-only thread log."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from weaver.fabric import FabricStore, ThreadConflict, ThreadQuery
from weaver.schema import ThreadDraft


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
    store.append(
        [
            draft("q", kind="요청", direction="받음"),
            draft("d", kind="결정", resolves="q"),
        ]
    )
    for bad in [
        draft("x", kind="결정", resolves="d"),
        draft("x", retracts="missing"),
        draft(
            "x",
            kind="태그정정",
            correction={"target": "missing", "add": [], "remove": []},
        ),
    ]:
        with pytest.raises(InvalidReference):
            store.append([bad])
    store.append([draft("r", retracts="d")])
    with pytest.raises(InvalidReference):
        store.append([draft("rr", retracts="r")])


def test_effective_tags_and_retracted_correction(tmp_path):
    store = FabricStore(tmp_path / "db")
    store.append(
        [
            draft("a", tags=["A"]),
            draft(
                "b",
                kind="태그정정",
                correction={"target": "a", "remove": ["A"], "add": ["B"]},
            ),
            draft(
                "c",
                kind="태그정정",
                correction={"target": "a", "remove": ["B"], "add": ["C"]},
            ),
        ]
    )
    assert [t.thread_id for t in store.entries(ThreadQuery(tag="C"))] == ["a"]
    store.append([draft("r", retracts="c")])
    assert [t.thread_id for t in store.entries(ThreadQuery(tag="B"))] == ["a"]
    assert store.entries(ThreadQuery(tag="C")) == []


def test_state_fields_assignees_and_open_requests(tmp_path):
    store = FabricStore(tmp_path / "db")
    store.append(
        [
            draft("q1", kind="요청", direction="받음"),
            draft("q2", kind="요청", direction="보냄"),
            draft("d", kind="결정", resolves="q1"),
            draft("a", field="status", value="old"),
            draft("b", field="status", value="new"),
            draft("s1", field="assignees", value={"add": ["A", "B"], "remove": []}),
            draft("s2", field="assignees", value={"add": ["C"], "remove": ["A"]}),
        ]
    )
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
    assert (
        store.contact_statistics("홍길동", since=datetime(2025, 1, 2, tzinfo=UTC)).count
        == 1
    )
    assert store.contact_statistics("홍길동").latest == datetime(2025, 1, 3, tzinfo=UTC)


def test_sqlite_rows_are_append_only(tmp_path):
    import sqlite3

    path = tmp_path / "db"
    store = FabricStore(path)
    store.append([draft("a")])
    with sqlite3.connect(path) as db:
        for sql in ("UPDATE threads SET payload='{}'", "DELETE FROM threads"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)


@pytest.mark.parametrize("field", ["deadline", "schedule", "status"])
def test_fields_follow_occurrence_order_and_ignore_retractions(tmp_path, field):
    store = FabricStore(tmp_path / "db")
    earlier = (
        {"start": "2025-01-01T00:00:00Z", "end": None, "timezone": "UTC"}
        if field == "schedule"
        else "earlier"
    )
    later = (
        {"start": "2025-01-03T00:00:00Z", "end": None, "timezone": "UTC"}
        if field == "schedule"
        else "later"
    )
    store.append(
        [
            draft(
                "later",
                field=field,
                value=later,
                occurred_at=datetime(2025, 1, 3, tzinfo=UTC),
            ),
            draft("earlier", field=field, value=earlier),
        ]
    )
    assert store.state("홍길동").fields[field].thread_id == "later"
    store.append([draft("retract", retracts="later")])
    assert store.state("홍길동").fields[field].value == earlier


def test_query_filters_use_effective_tags_and_time_bounds(tmp_path):
    store = FabricStore(tmp_path / "db")
    store.append(
        [
            draft("a", tags=["A"]),
            draft(
                "b",
                kind="태그정정",
                correction={"target": "a", "remove": ["A"], "add": ["B"]},
            ),
        ]
    )
    assert [
        t.thread_id
        for t in store.entries(
            ThreadQuery(
                kind="기록",
                tag="B",
                since=datetime(2025, 1, 1, tzinfo=UTC),
                until=datetime(2025, 1, 2, tzinfo=UTC),
            )
        )
    ] == ["a"]
    assert store.entries(ThreadQuery(kind="기록", tag="A")) == []


def test_config_selects_the_state_database(tmp_path):
    from types import SimpleNamespace

    config = SimpleNamespace(state_root=tmp_path)
    assert FabricStore.from_config(config).path.name == "fabric.sqlite3"


def test_schedule_block_reference_must_name_request(tmp_path):
    from weaver.fabric import InvalidReference

    store = FabricStore(tmp_path / "db")
    schedule = draft("s", kind="일정", blocked_by="q")
    with pytest.raises(InvalidReference):
        store.append([schedule])
    store.append([draft("q")])
    with pytest.raises(InvalidReference):
        store.append([schedule])
    store.append(
        [
            draft("request", kind="요청", direction="받음"),
            draft("ok", kind="일정", blocked_by="request"),
        ]
    )


def test_old_version_database_is_rejected(tmp_path):
    import sqlite3

    from weaver.fabric import SchemaVersionError

    path = tmp_path / "fabric.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE old_rows (id TEXT)")
        db.execute("PRAGMA user_version=1")
    with pytest.raises(SchemaVersionError):
        FabricStore(path)
    with sqlite3.connect(tmp_path / "unversioned.sqlite3") as db:
        db.execute("CREATE TABLE old_rows (id TEXT)")
    with pytest.raises(SchemaVersionError):
        FabricStore(tmp_path / "unversioned.sqlite3")


def execution(key, executes, at):
    """A 실행 Thread: the result of an Action, not a business fact."""
    return ThreadDraft(
        thread_id=key,
        kind="실행",
        content="calendar.register completed",
        occurred_at=at,
        recorded_at=at,
        tags=[],
        field=None,
        value={
            "action_id": "act_" + "a" * 32,
            "action": "calendar.register",
            "outcome": "completed",
            "external_id": "content://calendar/events/7",
            "error": None,
        },
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        actor="system",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
        executes=executes,
    )


def test_executions_are_hidden_unless_asked_and_must_point_at_a_fact(tmp_path):
    from weaver.fabric import InvalidReference

    store = FabricStore(tmp_path / "fabric.sqlite3")
    store.append([draft("fact")])
    done = execution("exe_1", "fact", datetime(2025, 1, 3, tzinfo=UTC))
    assert store.append([done])[0].seq == 2
    assert store.append([done])[0].seq == 2  # an identical replay is the same row
    assert [t.thread_id for t in store.entries(ThreadQuery())] == ["fact"]
    assert store.entries(ThreadQuery(after_seq=1)) == []
    assert [t.thread_id for t in store.entries(ThreadQuery(kind="실행"))] == ["exe_1"]
    both = store.entries(ThreadQuery(include_executions=True))
    assert [t.thread_id for t in both] == ["fact", "exe_1"]
    assert store.contact_statistics("홍길동").count == 1
    assert store.state("홍길동").fields == {}
    with pytest.raises(InvalidReference):
        store.append([execution("exe_2", "missing", datetime(2025, 1, 3, tzinfo=UTC))])
    with pytest.raises(InvalidReference):
        store.append([execution("exe_3", "exe_1", datetime(2025, 1, 3, tzinfo=UTC))])
    with pytest.raises(ThreadConflict):
        store.append([done.model_copy(update={"content": "calendar.register failed"})])


def test_updates_must_point_at_a_schedule_and_only_schedules_may_carry_it(tmp_path):
    from weaver.fabric import InvalidReference

    store = FabricStore(tmp_path / "fabric.sqlite3")
    value = {
        "start": "2026-12-01T10:00:00+09:00",
        "end": None,
        "timezone": "Asia/Seoul",
    }

    def sched(key, **changes):
        return draft(key, kind="일정", field="schedule", value=value, **changes)

    store.append([sched("s1"), draft("note")])
    assert store.append([sched("s2", updates="s1")])[0].seq == 3
    for target in ("missing", "note", "s9"):
        with pytest.raises(InvalidReference):
            store.append([sched("s3", updates=target)])
    with pytest.raises(ValueError):
        draft("x", updates="s1")
    with pytest.raises(ValueError):
        draft("x", kind="일정", updates="s1")
