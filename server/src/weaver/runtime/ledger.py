"""Per-source runtime progress and the extraction cache; never source text.

``state_root/runtime.sqlite3`` (0600, WAL, synchronous FULL, fullfsync) records
which archived Sources the resident loop finished (``done``), which wait for an
LLM (``waiting_llm``), which failed permanently (``failed``) and which retry
after a backoff (``retry``). Spinner, Loom and Courier keep their own durable
completion state; this ledger only avoids re-handing finished work to them.

The ``extractions`` table keeps each successful (paid) LLM extraction keyed by
``(source_id, extractor versions)`` before Proposer journals it, so a restart
after SIGKILL or a failed publication reuses it instead of calling again. It
holds model output derived from the source and is private like the journals.
"""

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

type Status = Literal["done", "waiting_llm", "failed", "retry"]

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        error_class TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        contract_failures INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS extractions (
        source_id TEXT NOT NULL,
        versions TEXT NOT NULL,
        extraction TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (source_id, versions)
    )""",
)


@dataclass(frozen=True)
class Progress:
    status: Status
    attempts: int
    contract_failures: int
    next_attempt_at: float | None


def versions_key(versions: Mapping[str, str] | None) -> str:
    return json.dumps(dict(versions or {}), sort_keys=True, separators=(",", ":"))


class RuntimeLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create private before SQLite opens it (umask-independent).
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600))
        os.chmod(path, 0o600)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            for statement in _SCHEMA:
                db.execute(statement)
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

    # ---- progress ----------------------------------------------------------------

    def final_ids(self) -> set[str]:
        """done/failed Sources: excluded from the archive query itself."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT source_id FROM sources WHERE status IN ('done', 'failed')"
            ).fetchall()
        return {row[0] for row in rows}

    def progress(self) -> dict[str, Progress]:
        """Non-final rows only (waiting_llm/retry)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT source_id, status, attempts, contract_failures,"
                " next_attempt_at FROM sources"
                " WHERE status NOT IN ('done', 'failed')"
            ).fetchall()
        return {row[0]: Progress(row[1], row[2], row[3], row[4]) for row in rows}

    def record(
        self,
        source_id: str,
        status: Status,
        *,
        attempts: int = 0,
        contract_failures: int = 0,
        error_class: str | None = None,
        next_attempt_at: float | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    status,
                    error_class,
                    attempts,
                    contract_failures,
                    next_attempt_at,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM sources GROUP BY status ORDER BY status"
            ).fetchall()
        return {status: count for status, count in rows}

    def failures(self, limit: int = 50) -> list[dict[str, str | None]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT source_id, error_class FROM sources WHERE status = 'failed'"
                " ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"source_id": sid, "error_class": error} for sid, error in rows]

    # ---- extraction cache --------------------------------------------------------

    def cached_extraction(
        self, source_id: str, versions: Mapping[str, str] | None
    ) -> str | None:
        """Serialized Extraction, ``"null"`` for "no extraction", None if absent."""
        with self._connect() as db:
            row = db.execute(
                "SELECT extraction FROM extractions"
                " WHERE source_id = ? AND versions = ?",
                (source_id, versions_key(versions)),
            ).fetchone()
        return None if row is None else str(row[0])

    def cache_extraction(
        self, source_id: str, versions: Mapping[str, str] | None, extraction: str
    ) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO extractions VALUES (?, ?, ?, ?)",
                (
                    source_id,
                    versions_key(versions),
                    extraction,
                    datetime.now(UTC).isoformat(),
                ),
            )
