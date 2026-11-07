"""Append-only local facts; state folding and execution eligibility are separate.

Times are supplied by the materializer, never rewritten on retry. v1 requires
known, timezone-aware occurred_at and recorded_at. Queries compare UTC instants,
using (occurred_at, recorded_at, entry_id), or (recorded_at, entry_id).
Bounds are inclusive since, exclusive until, on the selected ordering clock.
"""

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from weaver.config import ServerConfig
from weaver.schema import Entry
from weaver.schema.models import EntityId, EntryKind, EntrySource, Timestamp

SCHEMA_VERSION = 1
_REFERENCES = ("supersedes", "retracts", "resolves", "blocked_by_request")
_SCHEMA = (
    """CREATE TABLE entries (
        entry_id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL,
        source_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        supersedes TEXT REFERENCES entries(entry_id) DEFERRABLE INITIALLY DEFERRED,
        retracts TEXT REFERENCES entries(entry_id) DEFERRABLE INITIALLY DEFERRED,
        resolves TEXT REFERENCES entries(entry_id) DEFERRABLE INITIALLY DEFERRED,
        blocked_by_request TEXT REFERENCES entries(entry_id)
            DEFERRABLE INITIALLY DEFERRED,
        entry_json TEXT NOT NULL
    )""",
    """CREATE TABLE entry_entities (
        entry_id TEXT NOT NULL REFERENCES entries(entry_id),
        entity_id TEXT NOT NULL,
        PRIMARY KEY (entry_id, entity_id)
    )""",
    "CREATE INDEX entries_occurred ON entries(occurred_at, recorded_at, entry_id)",
    "CREATE INDEX entries_recorded ON entries(recorded_at, entry_id)",
    "CREATE INDEX entries_kind ON entries(kind, occurred_at, recorded_at, entry_id)",
    "CREATE INDEX entries_source ON entries(source_id, occurred_at, recorded_at, entry_id)",
    "CREATE INDEX entities_lookup ON entry_entities(entity_id, entry_id)",
    *(f"CREATE INDEX entries_{field} ON entries({field})" for field in _REFERENCES),
    *(
        f"""CREATE TRIGGER {table}_no_{operation.lower()}
        BEFORE {operation} ON {table}
        BEGIN SELECT RAISE(ABORT, 'Fabric is append-only'); END"""
        for table in ("entries", "entry_entities")
        for operation in ("UPDATE", "DELETE")
    ),
    # REPLACE can bypass DELETE triggers with recursive_triggers=OFF.
    """CREATE TRIGGER entries_no_replace BEFORE INSERT ON entries
        WHEN EXISTS (SELECT 1 FROM entries WHERE entry_id = NEW.entry_id)
        BEGIN SELECT RAISE(ABORT, 'Fabric duplicate entry'); END""",
    """CREATE TRIGGER entities_no_replace BEFORE INSERT ON entry_entities
        WHEN EXISTS (SELECT 1 FROM entry_entities
                     WHERE entry_id = NEW.entry_id AND entity_id = NEW.entity_id)
        BEGIN SELECT RAISE(ABORT, 'Fabric duplicate entity link'); END""",
)


class FabricError(Exception):
    """Storage failure; messages do not include fact content."""


class SchemaVersionError(FabricError):
    """An unsupported DB version must be migrated by a newer implementation."""


class EntryConflict(FabricError):
    """An ID was reused with different content, in storage or inside a batch."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        super().__init__("entry_id already has different content")


class InvalidReference(FabricError):
    """Structured reference error for callers; never includes Entry.value."""

    def __init__(self, entry_id: str, field: str, reason: str) -> None:
        self.entry_id = entry_id
        self.field = field
        self.reason = reason
        super().__init__(f"invalid {field}: {reason}")


class EntryQuery(BaseModel):
    """AND filters; UTC bounds apply to order_by, default occurrence time.

    Empty query returns all immutable facts, including retracted/superseded
    entries. No state folding or action permission is implied by this API.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    entity_id: EntityId | None = None
    kind: EntryKind | None = None
    source_id: EntrySource | None = None
    since: Timestamp | None = None
    until: Timestamp | None = None
    order_by: Literal["occurred_at", "recorded_at"] = "occurred_at"
    descending: bool = False

    @model_validator(mode="after")
    def time_range(self) -> "EntryQuery":
        if self.since is not None and self.until is not None:
            if self.since.astimezone(UTC) > self.until.astimezone(UTC):
                raise ValueError("since must not be after until")
        return self


class Fabric(Protocol):
    """Persistence boundary; state(entity_id) belongs to the state-folding issue."""

    def append(self, entries: list[Entry]) -> None: ...
    def entries(self, query: EntryQuery) -> list[Entry]: ...


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _canonical(entry: Entry) -> str:
    payload = entry.model_dump(mode="json")
    payload["occurred_at"] = _time(entry.occurred_at)
    payload["recorded_at"] = _time(entry.recorded_at)
    if entry.request is not None and entry.request.due_at is not None:
        payload["request"]["due_at"] = _time(entry.request.due_at)
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class FabricStore:
    """SQLite v1 store, one connection per call, safe across threads/processes.

    Production callers use from_config to select state_root/fabric.sqlite3,
    outside Courier and the vault. Explicit paths support isolated tests/tools.
    BEGIN IMMEDIATE serializes validation and writes; deferred FKs allow forward
    references within an atomic batch. No connections survive method return.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # journal_mode transitions can return SQLITE_BUSY without honoring the
        # busy timeout. Serialize initialization across processes and threads.
        with self._init_lock():
            self._initialize()

    @contextmanager
    def _init_lock(self) -> Iterator[None]:
        lock = self.path.with_name("." + self.path.name + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _initialize(self) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        os.close(fd)
        # Repair existing files before SQLite can read them or create sidecars.
        self._restrict_permissions()
        with self._connect() as db:
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise FabricError("SQLite WAL is unavailable")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise SchemaVersionError("unsupported Fabric database version")
            db.commit()
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        # WAL and SHM may still exist while another connection is open. Newly
        # created sidecars inherit the now-private DB mode, independent of umask.
        for suffix in ("", "-wal", "-shm"):
            with suppress(FileNotFoundError):
                os.chmod(f"{self.path}{suffix}", 0o600)

    @classmethod
    def from_config(cls, config: ServerConfig) -> "FabricStore":
        return cls(config.state_root / "fabric.sqlite3")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise FabricError("SQLite foreign keys are unavailable")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("PRAGMA checkpoint_fullfsync=ON")
            yield db
        except sqlite3.Error as exc:
            raise FabricError("Fabric SQLite operation failed") from exc
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def append(self, entries: list[Entry]) -> None:
        """Append all or none; identical retries preserve timestamps and rows.

        Replacement/retraction requires the same entity set and field. A
        retraction cannot itself be retracted. Request references target kind
        요청; resolves additionally requires matching entities and a human
        decision (enforced by Entry). All reference cycles are rejected.
        """
        batch: dict[str, Entry] = {}
        payloads: dict[str, str] = {}
        for supplied in entries:
            # Revalidate mutable or model_copy-constructed instances as well.
            item = Entry.model_validate(supplied.model_dump(mode="python"))
            payload = _canonical(item)
            if item.entry_id in payloads and payloads[item.entry_id] != payload:
                raise EntryConflict(item.entry_id)
            batch[item.entry_id] = item
            payloads[item.entry_id] = payload
        if not batch:
            return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh = {}
            for key, item in batch.items():
                row = db.execute(
                    "SELECT entry_json FROM entries WHERE entry_id=?", (key,)
                ).fetchone()
                if row is None:
                    fresh[key] = item
                elif row[0] != payloads[key]:
                    raise EntryConflict(key)
            self._validate_references(db, batch)
            for key, item in fresh.items():
                db.execute(
                    "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        item.kind,
                        item.source_id,
                        _time(item.occurred_at),
                        _time(item.recorded_at),
                        item.supersedes,
                        item.retracts,
                        item.resolves,
                        item.blocked_by_request,
                        payloads[key],
                    ),
                )
                db.executemany(
                    "INSERT INTO entry_entities VALUES (?, ?)",
                    [(key, entity) for entity in sorted(set(item.entities))],
                )
            db.commit()

    @staticmethod
    def _validate_references(db: sqlite3.Connection, batch: dict[str, Entry]) -> None:
        targets = dict(batch)
        edges: dict[str, set[str]] = {}
        for key, item in batch.items():
            if item.supersedes is not None and item.retracts is not None:
                raise InvalidReference(key, "supersedes/retracts", "mutually_exclusive")
            edges[key] = set()
            for field in _REFERENCES:
                target_id = getattr(item, field)
                if target_id is None:
                    continue
                if target_id == key:
                    raise InvalidReference(key, field, "self_reference")
                if target_id not in targets:
                    row = db.execute(
                        "SELECT entry_json FROM entries WHERE entry_id=?", (target_id,)
                    ).fetchone()
                    if row is None:
                        raise InvalidReference(key, field, "missing_target")
                    targets[target_id] = Entry.model_validate_json(row[0])
                target = targets[target_id]
                if target_id in batch:
                    edges[key].add(target_id)
                if field in ("supersedes", "retracts"):
                    if set(item.entities) != set(target.entities):
                        raise InvalidReference(key, field, "entity_mismatch")
                    if item.field != target.field:
                        raise InvalidReference(key, field, "field_mismatch")
                    if field == "retracts" and target.retracts is not None:
                        raise InvalidReference(key, field, "retraction_of_retraction")
                else:
                    if target.kind != "요청":
                        raise InvalidReference(key, field, "target_not_request")
                    if field == "resolves" and set(item.entities) != set(
                        target.entities
                    ):
                        raise InvalidReference(key, field, "entity_mismatch")
        # Kahn's algorithm avoids recursion limits on large valid batches.
        incoming = dict.fromkeys(edges, 0)
        for linked in edges.values():
            for target_id in linked:
                incoming[target_id] += 1
        ready = [key for key, count in incoming.items() if count == 0]
        visited = 0
        while ready:
            key = ready.pop()
            visited += 1
            for target_id in edges[key]:
                incoming[target_id] -= 1
                if incoming[target_id] == 0:
                    ready.append(target_id)
        if visited != len(edges):
            raise InvalidReference(
                next(k for k, n in incoming.items() if n), "references", "cycle"
            )

    def entries(self, query: EntryQuery) -> list[Entry]:
        """Return facts with AND filters and a stable, total UTC ordering."""
        # Validate copies constructed without Pydantic validation before SQL.
        query = EntryQuery.model_validate(query.model_dump())
        clauses, params = [], []
        for field in ("kind", "source_id"):
            value = getattr(query, field)
            if value is not None:
                clauses.append(f"e.{field} = ?")
                params.append(value)
        if query.entity_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM entry_entities t "
                "WHERE t.entry_id=e.entry_id AND t.entity_id=?)"
            )
            params.append(query.entity_id)
        for bound, operator in ((query.since, ">="), (query.until, "<")):
            if bound is not None:
                clauses.append(f"e.{query.order_by} {operator} ?")
                params.append(_time(bound))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        fields = ["occurred_at", "recorded_at", "entry_id"]
        if query.order_by == "recorded_at":
            fields = fields[1:]
        direction = " DESC" if query.descending else " ASC"
        order = ", ".join("e." + field + direction for field in fields)
        with self._connect() as db:
            rows = db.execute(
                "SELECT e.entry_json FROM entries e" + where + " ORDER BY " + order,
                params,
            ).fetchall()
        return [Entry.model_validate_json(row[0]) for row in rows]
