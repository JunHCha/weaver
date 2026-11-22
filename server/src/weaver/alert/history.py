"""Durable record of published alerts (``state_root/alerts.sqlite3``).

The phone deletes alert files it has dismissed or resolved, so file presence
cannot prevent repeats; this history does. History rows are never deleted.
Delivery bodies and per-device receipt acks are stored separately; an ack is
never a local dismiss.
"""

import contextlib
import os
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from weaver.schema import Alert


class AlertBodyExpired(ValueError):
    """The delivery ID exists, but its retained body has expired."""


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
                "CREATE TABLE IF NOT EXISTS alert_log ("
                "alert_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, "
                "thread_id TEXT NOT NULL, basis TEXT NOT NULL, "
                "created_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS alert_delivery ("
                "alert_id TEXT PRIMARY KEY REFERENCES alert_log(alert_id), "
                "body TEXT, projected INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS alert_ack ("
                "device_id TEXT NOT NULL, alert_id TEXT NOT NULL, "
                "received_at TEXT NOT NULL, PRIMARY KEY(device_id, alert_id))"
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
                "SELECT 1 FROM alert_log WHERE alert_id=?", (alert_id,)
            ).fetchone()
        return row is not None

    def count(self, package_id: str) -> int:
        with self._connect() as db:
            (count,) = db.execute(
                "SELECT COUNT(*) FROM alert_log WHERE package_id=?", (package_id,)
            ).fetchone()
        return int(count)

    def record(self, alert: Alert, basis: str) -> None:
        """Idempotent: a repeated record keeps the first row."""
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO alert_log VALUES (?,?,?,?,?)",
                (
                    alert.alert_id,
                    alert.package_id,
                    alert.thread_id,
                    basis,
                    alert.created_at.isoformat(),
                ),
            )

    def basis(self, alert_id: str) -> str | None:
        """Fabric Thread ID of the approved schedule the alert says may change."""
        with self._connect() as db:
            row = db.execute(
                "SELECT basis FROM alert_log WHERE alert_id=?", (alert_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def stage(self, alert: Alert, basis: str, *, projected: bool = False) -> None:
        """Commit body, history and pending publication together, before any delivery.

        Conflicting IDs retain the original body and the caller's file for diagnosis.
        A history-only ID is never synthesized; only an actual body may fill it.
        """
        body = alert.model_dump_json()
        with self._connect() as db:
            row = db.execute(
                "SELECT body FROM alert_delivery WHERE alert_id=?", (alert.alert_id,)
            ).fetchone()
            if row is not None:
                if row[0] is None:
                    db.execute(
                        "UPDATE alert_delivery SET body=?, projected=? WHERE alert_id=?",
                        (body, int(projected), alert.alert_id),
                    )
                    return
                if row[0] != body:
                    raise ValueError("alert_body_conflict")
                return
            db.execute(
                "INSERT OR IGNORE INTO alert_log VALUES (?,?,?,?,?)",
                (
                    alert.alert_id,
                    alert.package_id,
                    alert.thread_id,
                    basis,
                    alert.created_at.isoformat(),
                ),
            )
            db.execute(
                "INSERT INTO alert_delivery VALUES (?,?,?)",
                (alert.alert_id, body, int(projected)),
            )

    def body(self, alert_id: str) -> Alert | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT body FROM alert_delivery WHERE alert_id=?", (alert_id,)
            ).fetchone()
        if row is not None and row[0] is None:
            raise AlertBodyExpired(alert_id)
        return None if row is None else Alert.model_validate_json(row[0])

    def expired_count(self) -> int:
        with self._connect() as db:
            return int(
                db.execute(
                    "SELECT count(*) FROM alert_delivery WHERE body IS NULL"
                ).fetchone()[0]
            )

    def waiting_devices(self, devices: Sequence[str]) -> int:
        """Active recipients still missing at least one retained Alert receipt."""
        with self._connect() as db:
            return sum(
                bool(
                    db.execute(
                        "SELECT 1 FROM alert_delivery d WHERE d.body IS NOT NULL "
                        "AND NOT EXISTS (SELECT 1 FROM alert_ack a "
                        "WHERE a.alert_id=d.alert_id AND a.device_id=?) LIMIT 1",
                        (device,),
                    ).fetchone()
                )
                for device in devices
            )

    def bodies(
        self, *, pending: bool = False, device_id: str | None = None
    ) -> list[Alert]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT d.body FROM alert_delivery d WHERE d.body IS NOT NULL"
                + (" AND d.projected=0" if pending else "")
                + (
                    " AND NOT EXISTS (SELECT 1 FROM alert_ack a WHERE "
                    "a.alert_id=d.alert_id AND a.device_id=?)"
                    if device_id
                    else ""
                )
                + " ORDER BY d.alert_id",
                (device_id,) if device_id else (),
            ).fetchall()
        return [Alert.model_validate_json(row[0]) for row in rows]

    def projected(self, alert_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE alert_delivery SET projected=1 WHERE alert_id=?", (alert_id,)
            )

    def ack(self, device_id: str, alert_id: str, now: datetime) -> bool:
        """Idempotent receipt only; retain the body and other devices' delivery."""
        with self._connect() as db:
            row = db.execute(
                "SELECT body FROM alert_delivery WHERE alert_id=?", (alert_id,)
            ).fetchone()
            if row is None:
                return False
            if row[0] is None:
                raise AlertBodyExpired(alert_id)
            db.execute(
                "INSERT OR IGNORE INTO alert_ack VALUES (?,?,?)",
                (device_id, alert_id, now.astimezone(UTC).isoformat()),
            )
        return True

    def collect(self, days: int | None, devices: Sequence[str], now: datetime) -> int:
        """Expire bodies only after *every* current recipient acknowledged them.

        No recipient/ack means indefinite retention. History and acks survive GC.
        Call only in HTTP mode: files have no receipt-ack protocol.
        """
        if days is None or not devices:
            return 0
        cutoff = now.astimezone(UTC) - timedelta(days=days)
        removed = 0
        with self._connect() as db:
            rows = db.execute(
                "SELECT alert_id FROM alert_delivery "
                "WHERE body IS NOT NULL AND projected=1"
            ).fetchall()
            for (alert_id,) in rows:
                receipts = dict(
                    db.execute(
                        "SELECT device_id, received_at FROM alert_ack WHERE alert_id=?",
                        (alert_id,),
                    ).fetchall()
                )
                if all(
                    device in receipts
                    and datetime.fromisoformat(receipts[device]) <= cutoff
                    for device in devices
                ):
                    db.execute(
                        "UPDATE alert_delivery SET body=NULL WHERE alert_id=?",
                        (alert_id,),
                    )
                    removed += 1
        return removed
