"""Durable phone-API state: devices, the operation journal and the change feed.

One SQLite file (``mobile.sqlite3``, mode 0600) in ``state_root``. Tokens are
stored only as SHA-256 hashes. Journal rows keep opaque IDs, hashes and codes;
the validated request body is kept only until the operation is applied, so
that the resident runtime can finish it after a restart.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

SCOPES = ("read", "capture", "review", "action", "edit")
OperationKind = Literal["verdict", "action_result", "tag_edit", "dictionary_edit"]
OperationStatus = Literal["queued", "applying", "applied", "failed", "stale"]
TOKEN_PREFIX = "wvr_"
SCHEMA_VERSION = 1


class DeviceError(Exception):
    """A device or pairing request that cannot be honoured; the message is a code."""


@dataclass(frozen=True)
class Device:
    device_id: str
    label: str
    node_id: str
    login_name: str | None
    scopes: frozenset[str]
    executor: bool
    created_at: str
    revoked_at: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class Operation:
    operation_id: str
    kind: OperationKind
    device_id: str
    key: str
    content_hash: str
    status: OperationStatus
    error: str | None
    target_id: str
    target_revision: int | None
    payload: str | None
    result: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class FeedItem:
    seq: int
    kind: str
    resource_id: str
    revision: int | None
    op: Literal["upsert", "tombstone"]
    digest: str
    body: str | None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def content_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class MobileStore:
    def __init__(self, state_root: Path) -> None:
        self.path = state_root / "mobile.sqlite3"
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY, label TEXT NOT NULL,
                    node_id TEXT NOT NULL, login_name TEXT,
                    token_hash TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL,
                    executor INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, revoked_at TEXT);
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    device_id TEXT NOT NULL, key TEXT NOT NULL,
                    content_hash TEXT NOT NULL, status TEXT NOT NULL,
                    error TEXT, target_id TEXT NOT NULL, target_revision INTEGER,
                    payload TEXT, result TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE (kind, key));
                CREATE TABLE IF NOT EXISTS feed (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    resource_id TEXT NOT NULL, revision INTEGER, op TEXT NOT NULL,
                    digest TEXT NOT NULL, body TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS feed_resource
                    ON feed (kind, resource_id);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    device_id TEXT PRIMARY KEY, epoch TEXT NOT NULL,
                    seq INTEGER NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transports (
                    feature TEXT PRIMARY KEY, transport TEXT NOT NULL,
                    changed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS staged (
                    capture_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    created_at TEXT NOT NULL);
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('server_id', ?)",
                ("srv_" + uuid.uuid4().hex,),
            )
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('feed_epoch', ?)",
                ("ep_" + uuid.uuid4().hex,),
            )
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _meta(self, key: str) -> str:
        with self._connect() as db:
            return str(
                db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]
            )

    @property
    def server_id(self) -> str:
        return self._meta("server_id")

    @property
    def feed_epoch(self) -> str:
        return self._meta("feed_epoch")

    # ---- devices -------------------------------------------------------------

    def create_device(
        self,
        label: str,
        node_id: str,
        scopes: frozenset[str],
        *,
        login_name: str | None = None,
        executor: bool = False,
    ) -> tuple[Device, str]:
        """Return the device and its token; the token is shown exactly once."""
        if not label.strip() or not node_id.strip():
            raise DeviceError("invalid_device")
        if not scopes or scopes - set(SCOPES):
            raise DeviceError("invalid_scope")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)  # 256 bits
        device_id = "dev_" + uuid.uuid4().hex
        with self._write() as db:
            if executor:
                if db.execute(
                    "SELECT 1 FROM devices WHERE executor=1 AND revoked_at IS NULL"
                ).fetchone():
                    raise DeviceError("executor_exists")
                if "action" not in scopes:
                    raise DeviceError("executor_needs_action_scope")
            db.execute(
                "INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,NULL)",
                (
                    device_id,
                    label,
                    node_id,
                    login_name,
                    hash_token(token),
                    ",".join(sorted(scopes)),
                    int(executor),
                    now_iso(),
                ),
            )
        device = self.device(device_id)
        assert device is not None
        return device, token

    @staticmethod
    def _device(row: tuple[object, ...]) -> Device:
        return Device(
            device_id=str(row[0]),
            label=str(row[1]),
            node_id=str(row[2]),
            login_name=None if row[3] is None else str(row[3]),
            scopes=frozenset(str(row[5]).split(",")),
            executor=bool(row[6]),
            created_at=str(row[7]),
            revoked_at=None if row[8] is None else str(row[8]),
        )

    _DEVICE_COLUMNS = (
        "device_id, label, node_id, login_name, token_hash, scopes, executor,"
        " created_at, revoked_at"
    )

    def device(self, device_id: str) -> Device | None:
        with self._connect() as db:
            row = db.execute(
                f"SELECT {self._DEVICE_COLUMNS} FROM devices WHERE device_id=?",
                (device_id,),
            ).fetchone()
        return None if row is None else self._device(row)

    def device_for_token(self, token: str) -> Device | None:
        """Look up by hash; revoked devices are returned so callers can say why."""
        digest = hash_token(token)
        with self._connect() as db:
            row = db.execute(
                f"SELECT {self._DEVICE_COLUMNS} FROM devices WHERE token_hash=?",
                (digest,),
            ).fetchone()
        if row is None or not secrets.compare_digest(str(row[4]), digest):
            return None
        return self._device(row)

    def devices(self) -> list[Device]:
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {self._DEVICE_COLUMNS} FROM devices ORDER BY created_at"
            ).fetchall()
        return [self._device(row) for row in rows]

    def revoke(self, device_id: str) -> bool:
        with self._write() as db:
            cursor = db.execute(
                "UPDATE devices SET revoked_at=?, executor=0"
                " WHERE device_id=? AND revoked_at IS NULL",
                (now_iso(), device_id),
            )
            return cursor.rowcount == 1

    def executor(self) -> Device | None:
        with self._connect() as db:
            row = db.execute(
                f"SELECT {self._DEVICE_COLUMNS} FROM devices"
                " WHERE executor=1 AND revoked_at IS NULL"
            ).fetchone()
        return None if row is None else self._device(row)

    # ---- operation journal ---------------------------------------------------

    _OP_COLUMNS = (
        "operation_id, kind, device_id, key, content_hash, status, error, target_id,"
        " target_revision, payload, result, created_at, updated_at"
    )

    @staticmethod
    def _operation(row: tuple[object, ...]) -> Operation:
        return Operation(
            operation_id=str(row[0]),
            kind=str(row[1]),  # type: ignore[arg-type]
            device_id=str(row[2]),
            key=str(row[3]),
            content_hash=str(row[4]),
            status=str(row[5]),  # type: ignore[arg-type]
            error=None if row[6] is None else str(row[6]),
            target_id=str(row[7]),
            target_revision=None if row[8] is None else int(str(row[8])),
            payload=None if row[9] is None else str(row[9]),
            result=None if row[10] is None else str(row[10]),
            created_at=str(row[11]),
            updated_at=str(row[12]),
        )

    def operation(self, operation_id: str) -> Operation | None:
        with self._connect() as db:
            row = db.execute(
                f"SELECT {self._OP_COLUMNS} FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return None if row is None else self._operation(row)

    def find_operation(self, kind: OperationKind, key: str) -> Operation | None:
        with self._connect() as db:
            row = db.execute(
                f"SELECT {self._OP_COLUMNS} FROM operations WHERE kind=? AND key=?",
                (kind, key),
            ).fetchone()
        return None if row is None else self._operation(row)

    def enqueue(
        self,
        kind: OperationKind,
        device_id: str,
        key: str,
        digest: str,
        target_id: str,
        target_revision: int | None,
        payload: str,
        *,
        status: OperationStatus = "queued",
    ) -> tuple[Operation, bool]:
        """Insert once per ``(kind, key)``; return the row and whether it is new.

        The caller compares ``content_hash`` of an existing row to detect a
        conflicting retry. The row is the durable inbox: it commits before the
        request is acknowledged.
        """
        stamp = now_iso()
        with self._write() as db:
            row = db.execute(
                f"SELECT {self._OP_COLUMNS} FROM operations WHERE kind=? AND key=?",
                (kind, key),
            ).fetchone()
            if row is not None:
                return self._operation(row), False
            operation_id = "op_" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,NULL,?,?,?,NULL,?,?)",
                (
                    operation_id,
                    kind,
                    device_id,
                    key,
                    digest,
                    status,
                    target_id,
                    target_revision,
                    payload,
                    stamp,
                    stamp,
                ),
            )
        created = self.operation(operation_id)
        assert created is not None
        return created, True

    def update_operation(
        self,
        operation_id: str,
        status: OperationStatus,
        *,
        error: str | None = None,
        result: str | None = None,
        drop_payload: bool = False,
    ) -> None:
        with self._write() as db:
            db.execute(
                "UPDATE operations SET status=?, error=?, updated_at=?,"
                " result=COALESCE(?, result),"
                " payload=CASE WHEN ? THEN NULL ELSE payload END"
                " WHERE operation_id=?",
                (status, error, now_iso(), result, int(drop_payload), operation_id),
            )

    def stopped(self) -> list[Operation]:
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {self._OP_COLUMNS} FROM operations"
                " WHERE kind='verdict' AND status='failed' AND error='apply_stopped'"
            ).fetchall()
        return [self._operation(row) for row in rows]

    def pending(self, kind: OperationKind) -> list[Operation]:
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {self._OP_COLUMNS} FROM operations"
                " WHERE kind=? AND status IN ('queued','applying')"
                " ORDER BY created_at, operation_id",
                (kind,),
            ).fetchall()
        return [self._operation(row) for row in rows]

    # ---- change feed ---------------------------------------------------------

    def feed_head(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(MAX(seq), 0) FROM feed").fetchone()
            seq = int(row[0])
            floor = db.execute(
                "SELECT value FROM meta WHERE key='feed_floor'"
            ).fetchone()
        return max(seq, int(floor[0]) if floor else 0)

    def feed_current(self) -> dict[tuple[str, str], FeedItem]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT seq, kind, resource_id, revision, op, digest, body FROM feed"
            ).fetchall()
        return {
            (str(r[1]), str(r[2])): FeedItem(
                int(r[0]),
                str(r[1]),
                str(r[2]),
                None if r[3] is None else int(r[3]),
                r[4],
                str(r[5]),
                None if r[6] is None else str(r[6]),
            )
            for r in rows
        }

    def feed_record(self, items: list[FeedItem]) -> None:
        """Replace the latest entry of each resource in one transaction.

        Superseded rows are deleted (log compaction), so the feed from cursor 0
        is always a complete, bounded snapshot.
        """
        if not items:
            return
        with self._write() as db:
            for item in items:
                db.execute(
                    "DELETE FROM feed WHERE kind=? AND resource_id=?",
                    (item.kind, item.resource_id),
                )
                db.execute(
                    "INSERT INTO feed (kind, resource_id, revision, op, digest, body)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        item.kind,
                        item.resource_id,
                        item.revision,
                        item.op,
                        item.digest,
                        item.body,
                    ),
                )

    def feed_page(self, after: int, limit: int) -> tuple[list[FeedItem], bool]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT seq, kind, resource_id, revision, op, digest, body FROM feed"
                " WHERE seq > ? ORDER BY seq LIMIT ?",
                (after, limit + 1),
            ).fetchall()
        items = [
            FeedItem(
                int(r[0]),
                str(r[1]),
                str(r[2]),
                None if r[3] is None else int(r[3]),
                r[4],
                str(r[5]),
                None if r[6] is None else str(r[6]),
            )
            for r in rows
        ]
        return items[:limit], len(items) > limit

    def checkpoint(self, device_id: str) -> tuple[str, int] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT epoch, seq FROM checkpoints WHERE device_id=?", (device_id,)
            ).fetchone()
        return None if row is None else (str(row[0]), int(row[1]))

    def set_checkpoint(self, device_id: str, epoch: str, seq: int) -> None:
        with self._write() as db:
            db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(device_id)"
                " DO UPDATE SET epoch=excluded.epoch, updated_at=excluded.updated_at,"
                " seq=CASE WHEN checkpoints.epoch=excluded.epoch"
                " THEN MAX(checkpoints.seq, excluded.seq) ELSE excluded.seq END",
                (device_id, epoch, seq, now_iso()),
            )

    # ---- staging records ---------------------------------------------------------

    def stage(self, capture_id: str, device_id: str, sha256: str, size: int) -> None:
        with self._write() as db:
            db.execute(
                "INSERT OR REPLACE INTO staged VALUES (?,?,?,?,?)",
                (capture_id, device_id, sha256, size, now_iso()),
            )

    def staged(self, capture_id: str) -> tuple[str, str, int] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT device_id, sha256, size FROM staged WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]), int(row[2]))

    def unstage(self, capture_id: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM staged WHERE capture_id=?", (capture_id,))

    def expired_staged(self, older_than: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT capture_id FROM staged WHERE created_at < ?", (older_than,)
            ).fetchall()
        return [str(r[0]) for r in rows]

    # ---- transports ----------------------------------------------------------

    def recorded_transport(self, feature: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT transport FROM transports WHERE feature=?", (feature,)
            ).fetchone()
        return None if row is None else str(row[0])

    def record_transport(self, feature: str, transport: str) -> None:
        with self._write() as db:
            db.execute(
                "INSERT OR REPLACE INTO transports VALUES (?,?,?)",
                (feature, transport, now_iso()),
            )


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
