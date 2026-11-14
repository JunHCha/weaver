"""Durable Action outbox in SQLite, separate from the Fabric log.

Fabric holds immutable facts. An Action is a command whose execution state
changes (attempts, results), so it lives here. The outbox is derived: the
engine can enqueue the same Actions again from Fabric, and idempotency keys
make a replay a no-op. ``state_root/action-outbox.sqlite3`` is private (0600).
"""

import hashlib
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from weaver.schema import CalendarPayload
from weaver.schema.models import ActionKind

type Status = Literal["pending", "completed", "failed", "skipped"]
TERMINAL: tuple[Status, ...] = ("completed", "failed", "skipped")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS actions (
        action_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        source_id TEXT NOT NULL,
        input_version INTEGER NOT NULL,
        payload TEXT NOT NULL,
        lineage TEXT NOT NULL,
        position INTEGER NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL,
        error TEXT,
        external_id TEXT,
        UNIQUE (lineage, position)
    )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
)


def action_id_for(key: str) -> str:
    """Stable ID derived from the idempotency key, so a rebuilt outbox agrees."""
    return "act_" + hashlib.sha256(key.encode()).hexdigest()[:32]


def execution_thread_id(action_id: str) -> str:
    """The Fabric ``실행`` Thread that records this Action's result."""
    return "exe_" + action_id.removeprefix("act_")


@dataclass(frozen=True)
class NewAction:
    """An Action to enqueue. ``status`` is not pending only when a recorded
    execution already exists for it (a rebuilt outbox must not run it again).
    """

    kind: ActionKind
    thread_id: str
    seq: int
    source_id: str
    payload: CalendarPayload
    lineage: str
    position: int
    status: Status = "pending"
    attempts: int = 0
    error: str | None = None
    external_id: str | None = None

    @property
    def idempotency_key(self) -> str:
        return f"{self.kind}:{self.lineage}:{self.position}"

    @property
    def action_id(self) -> str:
        return action_id_for(self.idempotency_key)


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    idempotency_key: str
    kind: ActionKind
    thread_id: str
    seq: int
    source_id: str
    input_version: int
    payload: CalendarPayload
    lineage: str
    position: int
    status: Status
    attempts: int
    next_attempt_at: float | None
    error: str | None
    external_id: str | None


_COLUMNS = (
    "action_id, idempotency_key, kind, thread_id, seq, source_id, input_version,"
    " payload, lineage, position, status, attempts, next_attempt_at, error,"
    " external_id"
)


_KIND: TypeAdapter[ActionKind] = TypeAdapter(ActionKind)
_STATUS: TypeAdapter[Status] = TypeAdapter(Status)


def _record(row: Sequence[object]) -> ActionRecord:
    return ActionRecord(
        action_id=str(row[0]),
        idempotency_key=str(row[1]),
        kind=_KIND.validate_python(str(row[2])),
        thread_id=str(row[3]),
        seq=int(str(row[4])),
        source_id=str(row[5]),
        input_version=int(str(row[6])),
        payload=CalendarPayload.model_validate_json(str(row[7])),
        lineage=str(row[8]),
        position=int(str(row[9])),
        status=_STATUS.validate_python(str(row[10])),
        attempts=int(str(row[11])),
        next_attempt_at=None if row[12] is None else float(str(row[12])),
        error=None if row[13] is None else str(row[13]),
        external_id=None if row[14] is None else str(row[14]),
    )


class ActionOutbox:
    """Actions plus the Fabric cursor the engine has fully processed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600))
        os.chmod(path, 0o600)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            for statement in _SCHEMA:
                db.execute(statement)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous = FULL")
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    # ---- cursor and baseline -------------------------------------------------------

    def _meta(self, db: sqlite3.Connection, key: str) -> int | None:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else int(row[0])

    def cursor(self) -> int:
        with self._connect() as db:
            return self._meta(db, "cursor") or 0

    def baseline(self) -> int | None:
        """Fabric head when this outbox was created beside legacy schedule files.

        Schedules at or below it predate the Action path (the old phone flow may
        already have registered them) and are never enqueued. ``None`` means the
        outbox has not been initialized.
        """
        with self._connect() as db:
            return self._meta(db, "baseline")

    def initialize(self, baseline: int) -> None:
        """Record the baseline once; later calls keep the first value."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._meta(db, "baseline") is None:
                db.execute("INSERT INTO meta VALUES ('baseline', ?)", (baseline,))
                db.execute(
                    "INSERT OR IGNORE INTO meta VALUES ('cursor', ?)", (baseline,)
                )
            db.commit()

    def reset_cursor(self) -> None:
        """Replay Fabric from the start; idempotency keys prevent duplicates."""
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('cursor', 0)")

    # ---- enqueue ---------------------------------------------------------------------

    def commit(self, actions: Sequence[NewAction], cursor: int) -> int:
        """Enqueue the Actions and advance the cursor in one transaction.

        An Action whose idempotency key already exists is left untouched.
        Returns the number of Actions actually inserted.
        """
        inserted = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in actions:
                cur = db.execute(
                    f"INSERT INTO actions({_COLUMNS}) VALUES"
                    " (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)"
                    " ON CONFLICT(idempotency_key) DO NOTHING",
                    (
                        item.action_id,
                        item.idempotency_key,
                        item.kind,
                        item.thread_id,
                        item.seq,
                        item.source_id,
                        item.position + 1,
                        item.payload.model_dump_json(),
                        item.lineage,
                        item.position,
                        item.status,
                        item.attempts,
                        item.error,
                        item.external_id,
                    ),
                )
                inserted += cur.rowcount
            current = self._meta(db, "cursor") or 0
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('cursor', ?)",
                (max(current, cursor),),
            )
            db.commit()
        return inserted

    # ---- queries ---------------------------------------------------------------------

    def get(self, action_id: str) -> ActionRecord | None:
        with self._connect() as db:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return None if row is None else _record(row)

    def lineage(self, lineage: str) -> list[ActionRecord]:
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM actions WHERE lineage=? ORDER BY position",
                (lineage,),
            ).fetchall()
        return [_record(row) for row in rows]

    def all(self) -> list[ActionRecord]:
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM actions ORDER BY rowid"
            ).fetchall()
        return [_record(row) for row in rows]

    def dispatchable(self, now: float) -> list[tuple[ActionRecord, str | None]]:
        """Pending Actions the phone may run now, with the event they target.

        Within a lineage Actions run strictly in order, so only the first
        unfinished one is offered. An update or remove also needs the event the
        earlier register created.
        """
        offered: list[tuple[ActionRecord, str | None]] = []
        by_lineage: dict[str, list[ActionRecord]] = {}
        for record in self.all():
            by_lineage.setdefault(record.lineage, []).append(record)
        for records in by_lineage.values():
            records.sort(key=lambda r: r.position)
            event: str | None = None
            for record in records:
                if record.status == "completed" and record.kind != "calendar.remove":
                    event = record.external_id
                if record.status == "completed" and record.kind == "calendar.remove":
                    event = None
                if record.status != "pending":
                    continue
                if record.next_attempt_at is not None and record.next_attempt_at > now:
                    break
                if record.kind == "calendar.register":
                    offered.append((record, None))
                elif event is not None:
                    offered.append((record, event))
                break
        return sorted(offered, key=lambda item: item[0].seq)

    # ---- results ---------------------------------------------------------------------

    def finish(
        self,
        action_id: str,
        *,
        outcome: Literal["completed", "failed"],
        external_id: str | None,
        error: str | None,
        attempts: int,
    ) -> None:
        """Record a terminal result; a later Action that lost its target is skipped."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE actions SET status=?, external_id=?, error=?, attempts=?,"
                " next_attempt_at=NULL WHERE action_id=? AND status='pending'",
                (outcome, external_id, error, attempts, action_id),
            )
            row = db.execute(
                "SELECT lineage, position FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is not None and outcome == "failed":
                self._skip_orphans(db, row[0])
            db.commit()

    @staticmethod
    def _skip_orphans(db: sqlite3.Connection, lineage: str) -> None:
        """Mark updates and removes skipped when no event exists to act on."""
        rows = db.execute(
            "SELECT action_id, kind, status FROM actions WHERE lineage=?"
            " ORDER BY position",
            (lineage,),
        ).fetchall()
        has_event = False
        for action_id, kind, status in rows:
            if status == "completed":
                has_event = kind != "calendar.remove"
            elif status == "pending" and kind != "calendar.register" and not has_event:
                db.execute(
                    "UPDATE actions SET status='skipped', error='no_event'"
                    " WHERE action_id=?",
                    (action_id,),
                )

    def record_error(self, action_id: str, error: str, next_attempt_at: float) -> None:
        """A server-side failure to record a result; the Action stays pending."""
        with self._connect() as db:
            db.execute(
                "UPDATE actions SET attempts = attempts + 1, error=?,"
                " next_attempt_at=? WHERE action_id=? AND status='pending'",
                (error, next_attempt_at, action_id),
            )
