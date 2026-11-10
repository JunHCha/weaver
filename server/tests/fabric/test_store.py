import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from weaver.config import load_config
from weaver.fabric import (
    FabricError,
    FabricStore,
    InvalidReference,
    SchemaVersionError,
    ThreadConflict,
    ThreadQuery,
)
from weaver.schema import Thread


def entry(n: int = 1, **changes: object) -> Thread:
    return Thread.model_validate(
        dict(
            schema_version=1,
            entry_id=f"evt_{n:026d}",
            entities=[f"ent_{1:026d}", f"ent_{2:026d}"],
            kind="변경",
            field="deadline",
            value={"text": ["한글", 1, None]},
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            recorded_at=datetime(2026, 2, 1, tzinfo=UTC),
            source_id=f"src_{1:026d}",
            actor="ai_approved",
            supersedes=None,
            retracts=None,
            request=None,
            resolves=None,
            blocked_by_request=None,
        )
        | changes
    )


def test_roundtrip_and_retry(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    items = [entry(), entry(2, supersedes=entry().entry_id)]
    store.append(items)
    store.append(items)
    assert FabricStore(store.path).entries(ThreadQuery()) == items


def test_filters_and_half_open_ranges(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    a = entry()
    b = entry(2, entities=[f"ent_{3:026d}"], kind="결정", source_id="output:note")
    store.append([a, b])
    for entity_id in a.entities:
        assert store.entries(ThreadQuery(entity_id=entity_id)) == [a]
    assert store.entries(ThreadQuery(kind="결정", source_id="output:note")) == [b]
    assert store.entries(ThreadQuery(kind="변경", source_id="output:note")) == []
    assert store.entries(ThreadQuery(since=a.occurred_at)) == [a, b]
    assert store.entries(ThreadQuery(until=a.occurred_at)) == []
    assert store.entries(ThreadQuery(order_by="recorded_at", since=a.recorded_at)) == [
        a,
        b,
    ]
    assert store.entries(ThreadQuery(order_by="recorded_at", until=a.recorded_at)) == []
    with pytest.raises(ValidationError):
        ThreadQuery(since=a.recorded_at, until=a.occurred_at)


def test_late_arrival_timezone_and_ties(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    # Same instant, different timezone representation. IDs break final ties.
    a = entry(2)
    b = entry(1, occurred_at="2026-01-01T09:00:00+09:00")
    c = entry(3, occurred_at="2025-12-01T00:00:00Z", recorded_at="2026-03-01T00:00:00Z")
    d = entry(4, recorded_at="2026-01-15T00:00:00Z")
    store.append([a, b, d])
    store.append([c])
    assert store.entries(ThreadQuery()) == [c, d, b, a]
    assert store.entries(ThreadQuery(descending=True)) == [a, b, d, c]
    assert store.entries(ThreadQuery(order_by="recorded_at")) == [d, b, a, c]
    assert store.entries(ThreadQuery(order_by="recorded_at", descending=True)) == [
        c,
        a,
        b,
        d,
    ]
    assert store.entries(ThreadQuery(since="2026-01-01T09:00:00+09:00")) == [d, b, a]
    # Canonical UTC representation makes equivalent instants idempotent.
    store.append([entry(1)])
    assert len(store.entries(ThreadQuery())) == 4


@pytest.mark.parametrize("field", ["occurred_at", "recorded_at"])
@pytest.mark.parametrize("value", [None, "2026-01-01T00:00:00"])
def test_unknown_and_naive_times_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        entry(**{field: value})


def test_conflicts_in_storage_and_batch_are_atomic(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    store.append([entry()])
    for batch in (
        [entry(2), entry(value="different")],
        [entry(2), entry(2, actor="human")],
    ):
        with pytest.raises(ThreadConflict):
            store.append(batch)
        assert FabricStore(store.path).entries(ThreadQuery()) == [entry()]
    store.append([entry(), entry()])
    store.append([])
    assert store.entries(ThreadQuery()) == [entry()]


@pytest.mark.parametrize("table", ["entries", "entry_entities"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_sql_append_only_even_without_recursive_triggers(
    tmp_path: Path, table: str, operation: str
) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    store.append([entry()])
    statements = {
        "UPDATE": f"UPDATE {table} SET entry_id=entry_id",
        "DELETE": f"DELETE FROM {table}",
        "REPLACE": f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
    }
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA recursive_triggers=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statements[operation])
    assert store.entries(ThreadQuery()) == [entry()]


def test_mid_batch_sql_failure_rolls_back_rows_and_links(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON entries
                   WHEN NEW.entry_id = 'evt_00000000000000000000000002'
                   BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(FabricError):
        store.append([entry(), entry(2)])
    assert FabricStore(store.path).entries(ThreadQuery()) == []
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM entry_entities").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_second")
    store.append([entry(), entry(2)])
    assert len(store.entries(ThreadQuery())) == 2


@pytest.mark.parametrize(
    "field", ["supersedes", "retracts", "resolves", "blocked_by_request"]
)
def test_missing_and_self_references(tmp_path: Path, field: str) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    for target, reason in [
        (entry().entry_id, "self_reference"),
        (entry(9).entry_id, "missing_target"),
    ]:
        bad = entry(**{field: target, "kind": "결정", "actor": "human"})
        with pytest.raises(InvalidReference) as error:
            store.append([entry(2), bad])
        assert error.value.reason == reason
        assert error.value.field == field
        assert store.entries(ThreadQuery()) == []


@pytest.mark.parametrize("field", ["supersedes", "retracts"])
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"entities": [f"ent_{3:026d}"]}, "entity_mismatch"),
        ({"field": "name"}, "field_mismatch"),
    ],
)
def test_replacement_scope(
    tmp_path: Path, field: str, changes: dict[str, object], reason: str
) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    store.append([entry()])
    with pytest.raises(InvalidReference) as error:
        store.append([entry(2, **{field: entry().entry_id}, **changes)])
    assert error.value.reason == reason
    assert store.entries(ThreadQuery()) == [entry()]


def test_forward_chain_and_retraction_preserve_originals(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    original = entry()
    replacement = entry(
        2, entities=list(reversed(original.entities)), supersedes=original.entry_id
    )
    retraction = entry(3, retracts=replacement.entry_id)
    store.append([retraction, replacement, original])
    assert store.entries(ThreadQuery()) == [original, replacement, retraction]
    with pytest.raises(InvalidReference, match="retraction_of_retraction"):
        store.append([entry(4, retracts=retraction.entry_id)])
    with pytest.raises(InvalidReference, match="mutually_exclusive"):
        store.append(
            [entry(4, retracts=original.entry_id, supersedes=replacement.entry_id)]
        )
    with pytest.raises(InvalidReference, match="cycle"):
        store.append(
            [
                entry(5, supersedes=entry(6).entry_id),
                entry(6, supersedes=entry(5).entry_id),
            ]
        )
    assert FabricStore(store.path).entries(ThreadQuery()) == [
        original,
        replacement,
        retraction,
    ]


def request(n: int = 1) -> Thread:
    return entry(
        n,
        kind="요청",
        field=None,
        request={
            "requester": f"ent_{1:026d}",
            "targets": [f"ent_{2:026d}"],
            "due_at": None,
            "content": "합성 요청",
        },
    )


def test_request_links_and_human_resolution(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    req = request()
    decision = entry(2, kind="결정", actor="human", resolves=req.entry_id, field=None)
    blocked = entry(3, blocked_by_request=req.entry_id, entities=[f"ent_{3:026d}"])
    store.append([decision, blocked, req])
    assert store.entries(ThreadQuery()) == [req, decision, blocked]
    # Repeated decisions are immutable facts; interpretation is left to state folding.
    store.append([entry(4, kind="결정", actor="human", resolves=req.entry_id)])
    for field in ("resolves", "blocked_by_request"):
        with pytest.raises(InvalidReference, match="target_not_request"):
            store.append(
                [entry(5, kind="결정", actor="human", **{field: decision.entry_id})]
            )
    with pytest.raises(InvalidReference, match="entity_mismatch"):
        store.append(
            [entry(5, kind="결정", actor="human", resolves=req.entry_id, entities=[])]
        )
    with pytest.raises(ValidationError):
        entry(kind="결정", resolves=req.entry_id, actor="ai_approved")
    assert len(store.entries(ThreadQuery())) == 4


def test_mutated_entries_are_revalidated(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    bad = entry().model_copy(update={"occurred_at": None})
    with pytest.raises(ValidationError):
        store.append([entry(2), bad])
    assert store.entries(ThreadQuery()) == []


def test_concurrent_idempotent_writers(tmp_path: Path) -> None:
    path = tmp_path / "fabric.sqlite3"

    def write(_: int) -> None:
        FabricStore(path).append([entry(), entry(2)])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    assert FabricStore(path).entries(ThreadQuery()) == [entry(), entry(2)]


def test_schema_version_and_connection_settings(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    with store._connect() as db:
        assert sqlite3.sqlite_version_info >= (3, 6, 19)
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO entry_entities VALUES ('missing', 'missing')")
        db.execute("PRAGMA user_version=999")
    with pytest.raises(SchemaVersionError):
        FabricStore(store.path)


def test_config_location_is_private_state_root(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    roots = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")
    path.write_text("\n".join(f'{key} = "{key}"' for key in roots))
    config = load_config(path, environ={}, init_directories=True)
    store = FabricStore.from_config(config)
    assert store.path == config.state_root / "fabric.sqlite3"
    assert not store.path.is_relative_to(config.sync_root)
    assert not store.path.is_relative_to(config.vault_root)


@pytest.mark.parametrize(
    "value", [None, True, 3, 2.5, "문자", [1, False], {"a": {"b": 3}}]
)
def test_json_value_roundtrip(tmp_path: Path, value: object) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    item = entry(value=value)
    store.append([item])
    assert store.entries(ThreadQuery()) == [item]


def test_retry_normalizes_json_keys_and_request_due_timezone(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    raw = request().model_dump(mode="json")
    raw["request"]["due_at"] = "2026-03-01T09:00:00+09:00"
    raw["value"] = {"a": 1, "b": False}
    item = Thread.model_validate(raw)
    store.append([item])
    raw["request"]["due_at"] = "2026-03-01T00:00:00Z"
    raw["value"] = {"b": False, "a": 1}
    store.append([Thread.model_validate(raw)])
    assert store.entries(ThreadQuery()) == [item]


def test_dst_fold_range_uses_utc_instants(tmp_path: Path) -> None:
    zone = ZoneInfo("America/New_York")
    since = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    until = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    store = FabricStore(tmp_path / "fabric.sqlite3")
    items = [
        entry(i, occurred_at=f"2026-11-01T{instant}:00Z")
        for i, instant in enumerate(("05:44", "05:45", "06:00", "06:14", "06:15"), 1)
    ]
    store.append(items)
    assert store.entries(ThreadQuery(since=since, until=until)) == items[1:4]
    with pytest.raises(ValidationError, match="since must not be after until"):
        ThreadQuery(since=until, until=since)


def test_reopening_repairs_database_and_live_sidecar_permissions(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    store = FabricStore(state / "fabric.sqlite3")
    store.append([entry()])
    # Keep a connection open so WAL/SHM survive the store's reconnects.
    with closing(sqlite3.connect(store.path)) as observer:
        observer.execute("SELECT * FROM entries").fetchall()
        files = [Path(f"{store.path}{suffix}") for suffix in ("", "-wal", "-shm")]
        for file in files:
            file.chmod(0o644)
        reopened = FabricStore(store.path)
        assert reopened.entries(ThreadQuery()) == [entry()]
        for file in files:
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert not files[1].exists()
    assert not files[2].exists()
    # Sidecars recreated on later connections must inherit private DB permissions.
    with closing(sqlite3.connect(store.path)) as observer:
        observer.execute("SELECT * FROM entries").fetchall()
        for file in files:
            assert stat.S_IMODE(file.stat().st_mode) == 0o600


def test_mac_commit_and_checkpoint_fullfsync(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.sqlite3")
    with store._connect() as db:
        assert db.execute("PRAGMA fullfsync").fetchone()[0] == 1
        assert db.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
