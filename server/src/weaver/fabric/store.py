"""Append-only SQLite storage for version 2 Threads."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from weaver.config import ServerConfig
from weaver.schema.v2 import Thread, ThreadDraft

if TYPE_CHECKING:
    from .state import TagState


class FabricError(Exception):
    """Fabric storage error."""


class SchemaVersionError(FabricError):
    """Unsupported database version."""


class ThreadConflict(FabricError):
    """A thread identifier was reused with different content."""


class InvalidReference(FabricError):
    """A thread references an invalid target."""


class ThreadQuery(BaseModel):
    """Filters over occurrence time and the append sequence."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    after_seq: int = 0
    since: datetime | None = None
    until: datetime | None = None
    kind: str | None = None
    tag: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "ThreadQuery":
        if self.after_seq < 0:
            raise ValueError("after_seq must be nonnegative")
        if any(t is not None and t.utcoffset() is None for t in (self.since, self.until)):
            raise ValueError("timestamps must be timezone-aware")
        if self.since and self.until and self.since > self.until:
            raise ValueError("since must not be after until")
        return self


class Fabric(Protocol):
    """Append-only thread log."""

    def append(self, threads: list[ThreadDraft]) -> list[Thread]: ...
    def entries(self, query: ThreadQuery) -> list[Thread]: ...


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _canonical(item: ThreadDraft) -> str:
    data = item.model_dump(mode="json")
    data["occurred_at"] = _time(item.occurred_at)
    data["recorded_at"] = _time(item.recorded_at)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FabricStore:
    """SQLite log; each write is serialized in a single immediate transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise SchemaVersionError("unversioned database is not empty")
                db.execute("CREATE TABLE threads (seq INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)")
                for op in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER threads_no_{op.lower()} BEFORE {op} ON threads BEGIN SELECT RAISE(ABORT, 'append only'); END")
                db.execute("CREATE TRIGGER threads_no_replace BEFORE INSERT ON threads WHEN EXISTS (SELECT 1 FROM threads WHERE thread_id=NEW.thread_id OR seq=NEW.seq) BEGIN SELECT RAISE(ABORT, 'duplicate'); END")
                db.execute("PRAGMA user_version=2")
            elif version != 2:
                raise SchemaVersionError("unsupported Fabric database version")
            db.commit()
        self.path.chmod(0o600)

    @classmethod
    def from_config(cls, config: ServerConfig) -> "FabricStore":
        """Use the configured private state directory."""
        return cls(config.state_root / "fabric.sqlite3")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def append(self, threads: list[ThreadDraft]) -> list[Thread]:
        """Append all drafts atomically; identical retries return original seqs."""
        batch: dict[str, tuple[ThreadDraft, str]] = {}
        for supplied in threads:
            item = ThreadDraft.model_validate(supplied.model_dump(mode="python"))
            payload = _canonical(item)
            if item.thread_id in batch and batch[item.thread_id][1] != payload:
                raise ThreadConflict(item.thread_id)
            batch[item.thread_id] = (item, payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_references(db, batch)
            result: dict[str, Thread] = {}
            for key, (item, payload) in batch.items():
                row = db.execute("SELECT seq, payload FROM threads WHERE thread_id=?", (key,)).fetchone()
                if row is not None:
                    if row[1] != payload:
                        raise ThreadConflict(key)
                    seq = row[0]
                else:
                    seq = db.execute("INSERT INTO threads(thread_id, payload) VALUES (?, ?)", (key, payload)).lastrowid
                result[key] = Thread.model_validate({**item.model_dump(), "seq": seq})
            db.commit()
        return [result[t.thread_id] for t in threads]

    @staticmethod
    def _validate_references(db: sqlite3.Connection, batch: dict[str, tuple[ThreadDraft, str]]) -> None:
        targets = {key: item for key, (item, _) in batch.items()}
        edges: dict[str, set[str]] = {key: set() for key in batch}
        for key, (item, _) in batch.items():
            refs = [("resolves", item.resolves), ("retracts", item.retracts),
                    ("correction", item.correction.target if item.correction else None)]
            for field, target_id in refs:
                if target_id is None:
                    continue
                if target_id == key:
                    raise InvalidReference(f"{field}: self reference")
                if target_id not in targets:
                    row = db.execute("SELECT payload FROM threads WHERE thread_id=?", (target_id,)).fetchone()
                    if row is None:
                        raise InvalidReference(f"{field}: missing target")
                    targets[target_id] = ThreadDraft.model_validate_json(row[0])
                if target_id in batch:
                    edges[key].add(target_id)
                target = targets[target_id]
                if field == "resolves" and target.kind != "요청":
                    raise InvalidReference("resolves: target is not a request")
                if field == "retracts" and (target.kind == "거절" or target.retracts is not None):
                    raise InvalidReference("retracts: invalid target")
        remaining = {key: set(value) for key, value in edges.items()}
        while remaining:
            ready = {key for key, value in remaining.items() if not value}
            if not ready:
                raise InvalidReference("reference cycle")
            remaining = {key: value - ready for key, value in remaining.items() if key not in ready}

    def entries(self, query: ThreadQuery) -> list[Thread]:
        """Read immutable facts in occurrence, recording, sequence order."""
        query = ThreadQuery.model_validate(query.model_dump())
        with self._connect() as db:
            rows = db.execute("SELECT seq, payload FROM threads").fetchall()
        result = [Thread.model_validate({**json.loads(payload), "seq": seq}) for seq, payload in rows]
        result.sort(key=lambda t: (t.occurred_at, t.recorded_at, t.seq))
        retracted = {t.retracts for t in result if t.retracts is not None}
        effective = {t.thread_id: list(dict.fromkeys(t.tags)) for t in result}
        for t in result:
            if t.correction is not None and t.thread_id not in retracted and t.retracts is None:
                tags = effective[t.correction.target]
                tags[:] = [tag for tag in tags if tag not in t.correction.remove]
                tags.extend(tag for tag in t.correction.add if tag not in tags)
        result = [t.model_copy(update={"tags": effective[t.thread_id]}) for t in result]
        result = [t for t in result if t.seq > query.after_seq and (query.kind is None or t.kind == query.kind) and (query.since is None or t.occurred_at >= query.since) and (query.until is None or t.occurred_at < query.until) and (query.tag is None or query.tag in t.tags)]
        return sorted(result, key=lambda t: (t.occurred_at, t.recorded_at, t.seq))

    def state(self, tag: str) -> "TagState":
        """Return the current deterministic state for one exact tag."""
        from .state import project

        return project(tag, self.entries(ThreadQuery()))
