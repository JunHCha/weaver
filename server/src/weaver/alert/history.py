"""Durable record of published alerts (``state_root/alerts.sqlite3``).

The phone deletes alert files it has dismissed or resolved (#33), so file
presence cannot prevent repeats; this history does. Rows are never deleted.
Only IDs and Fabric entry IDs are stored, never source text.
"""

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from weaver.schema import Alert


class AlertHistory:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create private before SQLite opens it (umask-independent).
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS alerts ("
                "alert_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, "
                "rev INTEGER NOT NULL, schedule_ref TEXT NOT NULL, "
                "basis TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.chmod(str(path) + suffix, 0o600)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous = FULL")
            db.execute("PRAGMA fullfsync = ON")
            db.execute("PRAGMA checkpoint_fullfsync = ON")
            with db:
                yield db
        finally:
            db.close()

    def issued(self, alert_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM alerts WHERE alert_id=?", (alert_id,)
            ).fetchone()
        return row is not None

    def count(self, proposal_id: str) -> int:
        with self._connect() as db:
            (count,) = db.execute(
                "SELECT COUNT(*) FROM alerts WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return int(count)

    def record(self, alert: Alert, rev: int, basis: list[str]) -> None:
        """Idempotent: a repeated record keeps the first row."""
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?)",
                (
                    alert.alert_id,
                    alert.proposal_id,
                    rev,
                    alert.schedule_ref,
                    json.dumps(basis),
                    alert.created_at.isoformat(),
                ),
            )

    def basis(self, alert_id: str) -> list[str] | None:
        """Fabric entry IDs of the approved values the alert says may change."""
        with self._connect() as db:
            row = db.execute(
                "SELECT basis FROM alerts WHERE alert_id=?", (alert_id,)
            ).fetchone()
        return None if row is None else list(json.loads(row[0]))
