"""Per-source runtime progress; operational state only, never source content.

``state_root/runtime.sqlite3`` (0600, WAL, synchronous FULL, fullfsync) records
which archived Sources the resident loop has finished, which wait for an LLM,
which failed permanently and which retry after a backoff. Spinner, Loom and
Courier keep their own durable completion state; this ledger only avoids
re-handing finished work to them on every rescan.
"""

import contextlib
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

type Status = Literal["done", "waiting_llm", "failed", "retry"]


@dataclass(frozen=True)
class Progress:
    status: Status
    attempts: int
    next_attempt_at: float | None


class RuntimeLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create private before SQLite opens it (umask-independent).
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600))
        os.chmod(path, 0o600)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    error_class TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    updated_at TEXT NOT NULL
                )"""
            )
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.chmod(str(path) + suffix, 0o600)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(
            sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        ) as db:
            db.execute("PRAGMA synchronous = FULL")
            db.execute("PRAGMA fullfsync = ON")
            db.execute("PRAGMA checkpoint_fullfsync = ON")
            yield db

    def pragma(self, name: str) -> object:
        if not name.isidentifier():
            raise ValueError("invalid pragma name")
        with self._connect() as db:
            return db.execute(f"PRAGMA {name}").fetchone()[0]

    def progress(self) -> dict[str, Progress]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT source_id, status, attempts, next_attempt_at FROM sources"
            ).fetchall()
        return {row[0]: Progress(row[1], row[2], row[3]) for row in rows}

    def record(
        self,
        source_id: str,
        status: Status,
        *,
        error_class: str | None = None,
        next_attempt_at: float | None = None,
    ) -> int:
        """Store the outcome; returns the attempt count including this one."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT attempts FROM sources WHERE source_id = ?", (source_id,)
                ).fetchone()
                attempts = (row[0] if row else 0) + 1
                db.execute(
                    "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?)",
                    (source_id, status, error_class, attempts, next_attempt_at, now),
                )
            except BaseException:
                if db.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        return attempts

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM sources GROUP BY status ORDER BY status"
            ).fetchall()
        return {status: count for status, count in rows}
