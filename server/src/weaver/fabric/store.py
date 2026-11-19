"""Append-only SQLite storage for version 2 Threads."""

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from weaver.config import ServerConfig
from weaver.schema import Thread, ThreadDraft

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
    """Filters over occurrence time and the append sequence.

    ``실행`` Threads record what an Action did; they are not business facts, so
    readers skip them unless they set ``include_executions`` or ask for that
    kind. Wiki, Digest, queries and the pipeline map therefore never see them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    include_executions: bool = False
    after_seq: int = 0
    since: datetime | None = None
    until: datetime | None = None
    kind: str | None = None
    tag: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "ThreadQuery":
        if self.after_seq < 0:
            raise ValueError("after_seq must be nonnegative")
        if any(
            t is not None and t.utcoffset() is None for t in (self.since, self.until)
        ):
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
        self._readonly = False
        self.path = path.resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    raise SchemaVersionError("unversioned database is not empty")
                db.execute(
                    "CREATE TABLE threads (seq INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)"
                )
                for op in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER threads_no_{op.lower()} BEFORE {op} ON threads BEGIN SELECT RAISE(ABORT, 'append only'); END"
                    )
                db.execute(
                    "CREATE TRIGGER threads_no_replace BEFORE INSERT ON threads WHEN EXISTS (SELECT 1 FROM threads WHERE thread_id=NEW.thread_id OR seq=NEW.seq) BEGIN SELECT RAISE(ABORT, 'duplicate'); END"
                )
                db.execute("PRAGMA user_version=2")
            elif version != 2:
                raise SchemaVersionError("unsupported Fabric database version")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fabric_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO fabric_meta VALUES ('epoch', ?)",
                ("fab_" + uuid.uuid4().hex,),
            )
            db.commit()
        self.path.chmod(0o600)

    @classmethod
    def from_config(cls, config: ServerConfig) -> "FabricStore":
        """Use the configured private state directory."""
        return cls(config.state_root / "fabric.sqlite3")

    @classmethod
    def open_readonly(cls, path: Path) -> "FabricStore":
        """Open existing state without initialization, DDL or permission changes."""
        if not path.is_file():
            raise FileNotFoundError(path)
        store = cls.__new__(cls)
        store.path = path.resolve()
        store._readonly = True
        return store

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = (
            sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=0.1,
                isolation_level=None,
            )
            if self._readonly
            else sqlite3.connect(self.path, timeout=30, isolation_level=None)
        )
        try:
            if self._readonly and db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise SchemaVersionError("unsupported Fabric database version")
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def append(self, threads: list[ThreadDraft]) -> list[Thread]:
        """Append all drafts atomically; identical retries return original seqs."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._append_in(db, threads)
            db.commit()
            return result

    def _epoch_in(self, db: sqlite3.Connection) -> str:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='fabric_meta'"
        ).fetchone():
            # Read old databases without migrating on a GET; their next writer
            # installs a durable epoch, resetting old view bookmarks once.
            info = self.path.stat()
            return (
                f"legacy-{info.st_dev}-{info.st_ino}-{getattr(info, 'st_birthtime', 0)}"
            )
        return str(
            db.execute("SELECT value FROM fabric_meta WHERE key='epoch'").fetchone()[0]
        )

    def epoch(self) -> str:
        """A durable generation ID, replaced together with the Fabric database."""
        with self._connect() as db:
            return self._epoch_in(db)

    def read_snapshot(self) -> tuple[list[Thread], str]:
        """Read the full log and its generation from one SQLite snapshot."""
        with self._connect() as db:
            db.execute("BEGIN")
            return self._entries_in(
                db, ThreadQuery(include_executions=True)
            ), self._epoch_in(db)

    def edit(self, build: Callable[[list[Thread]], list[ThreadDraft]]) -> list[Thread]:
        """Validate against the entire log and append under the same write lock."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._append_in(
                db, build(self._entries_in(db, ThreadQuery(include_executions=True)))
            )
            db.commit()
            return result

    def web_edit(
        self, build: Callable[[list[Thread], set[str]], list[ThreadDraft]]
    ) -> list[Thread]:
        """Build against current effective rows and append under one write lock.

        Provenance lives beside the immutable payload, without changing the shared
        Thread schema. Older or non-web corrections are deliberately unmarked.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='web_tag_corrections'"
            ).fetchone()
            marked = (
                {
                    row[0]
                    for row in db.execute("SELECT thread_id FROM web_tag_corrections")
                }
                if exists
                else set()
            )
            drafts = build(
                self._entries_in(db, ThreadQuery(include_executions=True)), marked
            )
            result = self._append_in(db, drafts)
            if drafts:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS web_tag_corrections (thread_id TEXT PRIMARY KEY)"
                )
            if drafts:
                db.executemany(
                    "INSERT OR IGNORE INTO web_tag_corrections VALUES (?)",
                    [(row.thread_id,) for row in result if row.correction is not None],
                )
            db.commit()
            return result

    def _append_in(
        self, db: sqlite3.Connection, threads: list[ThreadDraft]
    ) -> list[Thread]:
        batch: dict[str, tuple[ThreadDraft, str]] = {}
        for supplied in threads:
            item = ThreadDraft.model_validate(supplied.model_dump(mode="python"))
            payload = _canonical(item)
            if item.thread_id in batch and batch[item.thread_id][1] != payload:
                raise ThreadConflict(item.thread_id)
            batch[item.thread_id] = (item, payload)
        self._validate_references(db, batch)
        result: dict[str, Thread] = {}
        for key, (item, payload) in batch.items():
            row = db.execute(
                "SELECT seq, payload FROM threads WHERE thread_id=?", (key,)
            ).fetchone()
            if row is not None:
                if _canonical(ThreadDraft.model_validate_json(row[1])) != payload:
                    raise ThreadConflict(key)
                seq = row[0]
            else:
                seq = db.execute(
                    "INSERT INTO threads(thread_id, payload) VALUES (?, ?)",
                    (key, payload),
                ).lastrowid
            result[key] = Thread.model_validate({**item.model_dump(), "seq": seq})
        return [result[t.thread_id] for t in threads]

    @staticmethod
    def _validate_references(
        db: sqlite3.Connection, batch: dict[str, tuple[ThreadDraft, str]]
    ) -> None:
        targets = {key: item for key, (item, _) in batch.items()}
        edges: dict[str, set[str]] = {key: set() for key in batch}
        for key, (item, _) in batch.items():
            refs = [
                ("resolves", item.resolves),
                ("blocked_by", item.blocked_by),
                ("retracts", item.retracts),
                ("correction", item.correction.target if item.correction else None),
                ("executes", item.executes),
                ("updates", item.updates),
                ("amends", item.amends),
            ]
            for field, target_id in refs:
                if target_id is None:
                    continue
                if target_id == key:
                    raise InvalidReference(f"{field}: self reference")
                if target_id not in targets:
                    row = db.execute(
                        "SELECT payload FROM threads WHERE thread_id=?", (target_id,)
                    ).fetchone()
                    if row is None:
                        raise InvalidReference(f"{field}: missing target")
                    targets[target_id] = ThreadDraft.model_validate_json(row[0])
                if target_id in batch:
                    edges[key].add(target_id)
                target = targets[target_id]
                if field in ("resolves", "blocked_by") and target.kind != "요청":
                    raise InvalidReference(f"{field}: target is not a request")
                if field == "retracts" and (
                    target.kind == "거절" or target.retracts is not None
                ):
                    raise InvalidReference("retracts: invalid target")
                if field == "amends" and (
                    target.kind != "요청" or target.retracts is not None
                ):
                    raise InvalidReference("amends: target is not a request fact")
                if field == "updates" and target.kind != "일정":
                    raise InvalidReference("updates: target is not a schedule")
                if field == "executes" and target.kind in ("실행", "태그정정", "거절"):
                    raise InvalidReference("executes: target is not a business fact")
        remaining = {key: set(value) for key, value in edges.items()}
        while remaining:
            ready = {key for key, value in remaining.items() if not value}
            if not ready:
                raise InvalidReference("reference cycle")
            remaining = {
                key: value - ready
                for key, value in remaining.items()
                if key not in ready
            }

    def entries(self, query: ThreadQuery) -> list[Thread]:
        """Read immutable facts in occurrence, recording, sequence order."""
        query = ThreadQuery.model_validate(query.model_dump())
        with self._connect() as db:
            return self._entries_in(db, query)

    @staticmethod
    def _entries_in(db: sqlite3.Connection, query: ThreadQuery) -> list[Thread]:
        rows = db.execute("SELECT seq, payload FROM threads").fetchall()
        result = [
            Thread.model_validate({**json.loads(payload), "seq": seq})
            for seq, payload in rows
        ]
        result.sort(key=lambda t: (t.occurred_at, t.recorded_at, t.seq))
        retracted = {t.retracts for t in result if t.retracts is not None}
        effective = {t.thread_id: list(dict.fromkeys(t.tags)) for t in result}
        for t in result:
            if (
                t.correction is not None
                and t.thread_id not in retracted
                and t.retracts is None
            ):
                tags = effective[t.correction.target]
                tags[:] = [tag for tag in tags if tag not in t.correction.remove]
                tags.extend(tag for tag in t.correction.add if tag not in tags)
        result = [t.model_copy(update={"tags": effective[t.thread_id]}) for t in result]
        result = [
            t
            for t in result
            if t.seq > query.after_seq
            and (t.kind != "실행" or query.include_executions or query.kind == "실행")
            and (query.kind is None or t.kind == query.kind)
            and (query.since is None or t.occurred_at >= query.since)
            and (query.until is None or t.occurred_at < query.until)
            and (query.tag is None or query.tag in t.tags)
        ]
        return sorted(result, key=lambda t: (t.occurred_at, t.recorded_at, t.seq))

    def state(self, tag: str) -> "TagState":
        """Return the current deterministic state for one exact tag."""
        from .state import project

        return project(tag, self.entries(ThreadQuery()))

    def contact_statistics(
        self, tag: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> "ContactStatistics":
        """Count active events for a tag and find their latest occurrence."""
        snapshot = self.entries(ThreadQuery())
        retracted = {t.retracts for t in snapshot if t.retracts is not None}
        events = [
            t
            for t in snapshot
            if tag in t.tags
            and t.thread_id not in retracted
            and t.retracts is None
            and t.kind not in ("태그정정", "거절", "실행")
            and (since is None or t.occurred_at >= since)
            and (until is None or t.occurred_at < until)
        ]
        return ContactStatistics(
            len(events), max((t.occurred_at for t in events), default=None)
        )


@dataclass(frozen=True)
class ContactStatistics:
    """Number of active events and latest occurrence in the selected interval."""

    count: int
    latest: datetime | None
