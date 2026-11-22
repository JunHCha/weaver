"""Shared private capture journal: buffers, immutable requests and Receipt recovery.

Cursor updates commit with buffers. Only a validated, durably recorded Receipt
allows payload GC; quarantine and authentication failures retain original bytes.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
from pydantic import ValidationError

from weaver.schema import Manifest, Receipt

from .settings import OutboxLimits as OutboxLimits

logger = logging.getLogger("weaver.shepherd")
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@dataclass(frozen=True)
class BundleKey:
    instance_id: str
    space: str
    day: str
    part: int
    revision: int

    @property
    def encoded(self) -> bytes:
        if not self.instance_id.strip() or not self.space.strip():
            raise ValueError("invalid_bundle_identity")
        if date.fromisoformat(self.day).isoformat() != self.day:
            raise ValueError("invalid_bundle_date")
        if any(type(v) is not int or v < 0 for v in (self.part, self.revision)):
            raise ValueError("invalid_bundle_revision")
        values = (
            self.instance_id,
            self.space,
            self.day,
            str(self.part),
            str(self.revision),
        )
        encoded = [unicodedata.normalize("NFC", v).encode("utf-8") for v in values]
        return b"".join(len(v).to_bytes(4, "big") + v for v in encoded)

    @property
    def capture_id(self) -> str:
        number = int.from_bytes(hashlib.sha256(self.encoded).digest()[:16], "big")
        result = ""
        for _ in range(26):
            result = _ALPHABET[number & 31] + result
            number >>= 5
        return "cap_" + result


class CapacityError(RuntimeError):
    """Stop source collection and cursor advancement; operator action required."""


@dataclass(frozen=True)
class Entry:
    capture_id: str
    state: str
    payload: bytes | None
    manifest: bytes | None
    receipt: bytes | None
    depends_on: str | None
    error: str | None


class Outbox:
    def __init__(
        self,
        path: Path,
        instance_id: str,
        limits: OutboxLimits | None = None,
        *,
        free_bytes: Callable[[], int] | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        if not instance_id.strip():
            raise ValueError("invalid_instance_id")
        path = path.absolute()
        self.path, self.instance_id, self.limits = (
            path,
            instance_id,
            limits or OutboxLimits(),
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.parent.resolve() != path.parent.absolute():
            raise ValueError("unsafe_outbox_path")
        if path.parent.stat().st_mode & 0o077:
            raise ValueError("unsafe_outbox_permissions")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("unsafe_outbox_file")
        finally:
            os.close(fd)
        self.free_bytes = free_bytes or (lambda: shutil.disk_usage(path.parent).free)
        self.notify = notify or (
            lambda code: logger.warning("Shepherd stopped: %s", code)
        )
        with self._connect() as db:
            # Effective immediately for new databases; legacy files need offline VACUUM.
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        with self._write() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS entries (
                capture_id TEXT PRIMARY KEY, bundle BLOB NOT NULL UNIQUE,
                state TEXT NOT NULL, payload BLOB, manifest BLOB, receipt BLOB,
                depends_on TEXT, error TEXT)""")
            row = db.execute(
                "SELECT value FROM meta WHERE key='instance_id'"
            ).fetchone()
            if row and row[0] != instance_id:
                raise ValueError("outbox_instance_mismatch")
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('instance_id', ?)", (instance_id,)
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("PRAGMA secure_delete=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(
            self.path.as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None
        )
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def _capacity(
        self,
        db: sqlite3.Connection,
        added_items: int,
        added_bytes: int,
        *,
        collecting: bool = False,
    ) -> None:
        count, size = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload) + COALESCE(LENGTH(manifest), 0)), 0) FROM entries WHERE state IN ('buffer','pending','quarantine')"
        ).fetchone()
        if (
            (
                collecting
                and (count >= self.limits.max_items or size >= self.limits.max_bytes)
            )
            or count + added_items > self.limits.max_items
            or size + added_bytes > self.limits.max_bytes
            or self.free_bytes() - max(0, added_bytes) < self.limits.min_free_bytes
        ):
            self.notify("outbox_capacity")
            raise CapacityError("outbox_capacity")

    def buffer(
        self, key: BundleKey, payload: bytes, *, cursor: tuple[str, str] | None = None
    ) -> None:
        self.buffer_page([(key, payload)], cursor=cursor)

    def buffer_page(
        self,
        buffers: list[tuple[BundleKey, bytes]],
        *,
        cursor: tuple[str, str] | None = None,
        exclusions: tuple[str, ...] | None = None,
    ) -> None:
        """Commit every bundle touched by one source page with its source cursor."""
        with self._write() as db:
            if exclusions is not None:
                db.execute(
                    "INSERT OR REPLACE INTO cursors VALUES (?,?)",
                    ("webex.excluded_spaces.v1", json.dumps(exclusions)),
                )
            for key, payload in buffers:
                if key.instance_id != self.instance_id:
                    raise ValueError("outbox_instance_mismatch")
                row = db.execute(
                    "SELECT bundle, state, LENGTH(payload) FROM entries WHERE capture_id=?",
                    (key.capture_id,),
                ).fetchone()
                if row and (row[0] != key.encoded or row[1] != "buffer"):
                    raise ValueError("immutable_capture")
                self._capacity(
                    db,
                    int(row is None),
                    len(payload) - (row[2] if row else 0),
                    collecting=True,
                )
                db.execute(
                    "INSERT INTO entries VALUES (?,?,'buffer',?,NULL,NULL,NULL,NULL) ON CONFLICT(capture_id) DO UPDATE SET payload=excluded.payload",
                    (key.capture_id, key.encoded, payload),
                )
            if cursor:
                if not buffers:
                    self._capacity(db, 0, 0, collecting=True)
                db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", cursor)

    def cursor(self, name: str) -> str | None:
        with self._read() as db:
            row = db.execute(
                "SELECT value FROM cursors WHERE name=?", (name,)
            ).fetchone()
        return None if row is None else str(row[0])

    def space_excluded(self, space: str) -> bool:
        """Stage 7 must consult the durable collection exclusion before close."""
        return space in json.loads(self.cursor("webex.excluded_spaces.v1") or "[]")

    def close(
        self, key: BundleKey, manifest: Manifest, *, depends_on: str | None = None
    ) -> None:
        raw = manifest.model_dump_json().encode()
        manifest = Manifest.model_validate_json(raw)
        with self._write() as db:
            excluded = db.execute(
                "SELECT value FROM cursors WHERE name='webex.excluded_spaces.v1'"
            ).fetchone()
            if excluded and key.space in json.loads(excluded[0]):
                raise ValueError("excluded_space")
            row = db.execute(
                "SELECT bundle, state, payload, manifest, depends_on FROM entries WHERE capture_id=?",
                (key.capture_id,),
            ).fetchone()
            if (
                key.instance_id != self.instance_id
                or row is None
                or row[0] != key.encoded
            ):
                raise ValueError("unknown_bundle")
            if row[1] != "buffer":
                if row[3] == raw and row[4] == depends_on:
                    return
                raise ValueError("immutable_capture")
            body = bytes(row[2])
            if (
                manifest.capture_id != key.capture_id
                or manifest.size != len(body)
                or manifest.sha256 != hashlib.sha256(body).hexdigest()
                or len(body) > 64 * 1024**2
                or len(raw) > 64 * 1024
                or not manifest.payload.startswith(key.capture_id + ".")
                or "/" in manifest.payload
                or manifest.payload.endswith(".json")
            ):
                raise ValueError("invalid_frozen_request")
            if key.revision > 0 and depends_on is None:
                raise ValueError("correction_requires_dependency")
            if depends_on:
                parent = db.execute(
                    "SELECT bundle FROM entries WHERE capture_id=? AND state != 'buffer'",
                    (depends_on,),
                ).fetchone()
                if depends_on == key.capture_id or parent is None:
                    raise ValueError("invalid_dependency")
                if key.revision > 0:
                    prefix = key.encoded[: -(4 + len(str(key.revision).encode()))]
                    previous = db.execute(
                        "SELECT capture_id, bundle, state FROM entries WHERE substr(bundle,1,?)=? AND capture_id!=? AND state!='discarded'",
                        (len(prefix), prefix, key.capture_id),
                    ).fetchall()
                    earlier = [
                        (int(bytes(bundle)[len(prefix) + 4 :]), identifier, state)
                        for identifier, bundle, state in previous
                        if int(bytes(bundle)[len(prefix) + 4 :]) < key.revision
                    ]
                    if (
                        not earlier
                        or max(earlier)[1] != depends_on
                        or max(earlier)[2] == "buffer"
                    ):
                        raise ValueError("invalid_correction_dependency")
            self._capacity(db, 0, len(raw))
            db.execute(
                "UPDATE entries SET state='pending', manifest=?, depends_on=? WHERE capture_id=?",
                (raw, depends_on, key.capture_id),
            )

    def get(self, capture_id: str, *, include_payload: bool = True) -> Entry:
        """Read one request; metadata callers explicitly omit the payload BLOB."""
        payload = "payload" if include_payload else "NULL"
        with self._read() as db:
            row = db.execute(
                f"SELECT capture_id, state, {payload}, manifest, receipt, depends_on, error FROM entries WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown_capture")
        return Entry(*row)

    def entries(self, *, include_received: bool = True) -> Iterator[Entry]:
        """Metadata only: listing never reads payload BLOBs."""
        with self._read() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT capture_id FROM entries WHERE (? OR state!='received') ORDER BY rowid",
                    (include_received,),
                )
            ]
        for identifier in ids:
            yield self.get(identifier, include_payload=False)

    def counts(self) -> dict[str, int]:
        with self._read() as db:
            return dict(
                db.execute("SELECT state, COUNT(*) FROM entries GROUP BY state")
            )

    @property
    def paused(self) -> bool:
        with self._read() as db:
            return (
                db.execute("SELECT 1 FROM meta WHERE key='auth_paused'").fetchone()
                is not None
            )

    def resume(self) -> None:
        """After operator re-registration; next delivery probes Receipt first."""
        with self._write() as db:
            db.execute("DELETE FROM meta WHERE key='auth_paused'")

    def retry(self, capture_id: str) -> None:
        with self._write() as db:
            if not db.execute(
                "UPDATE entries SET state='pending', error=NULL WHERE capture_id=? AND state='quarantine'",
                (capture_id,),
            ).rowcount:
                raise ValueError("not_quarantined")

    def discard(self, capture_id: str) -> None:
        """Explicit operator disposal only; retain ID tombstone and dependency block."""
        with self._write() as db:
            if not db.execute(
                "UPDATE entries SET state='discarded', payload=NULL, manifest=NULL WHERE capture_id=? AND state IN ('buffer','pending','quarantine')",
                (capture_id,),
            ).rowcount:
                raise ValueError("not_discardable")
        self._compact()

    @staticmethod
    def _payload_missing(response: httpx.Response) -> bool:
        if response.status_code != 409:
            return False
        try:
            detail = response.json()
        except ValueError:
            return False
        return (
            isinstance(detail, dict)
            and detail.get("code") == "payload_missing"
            and detail.get("retryable") is True
        )

    def _refused(
        self, entry: Entry, response: httpx.Response, *, receipt_probe: bool = False
    ) -> bool:
        status = response.status_code
        if status in (200, 201, 408, 429) or 500 <= status <= 599:
            return False
        # A missing Receipt is the one expected 404; uploads must not retry 404.
        if receipt_probe and status == 404:
            return False
        try:
            detail = response.json()
        except ValueError:
            detail = {}
        if (
            status == 409
            and isinstance(detail, dict)
            and detail.get("code") == "payload_missing"
            and detail.get("retryable") is True
        ):
            return False
        code = detail.get("code") if isinstance(detail, dict) else None
        error = f"http_{status}"
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            error += ":" + code
        with self._write() as db:
            if status in (409, 422, 413):
                db.execute(
                    "UPDATE entries SET state='quarantine', error=? WHERE capture_id=? AND state='pending'",
                    (error, entry.capture_id),
                )
            else:
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('auth_paused', ?)", (error,)
                )
                db.execute(
                    "UPDATE entries SET error=? WHERE capture_id=? AND state='pending'",
                    (error, entry.capture_id),
                )
        self.notify(
            "courier_quarantine"
            if status in (409, 422, 413)
            else "courier_auth_required"
            if status in (401, 403)
            else "courier_operator_required"
        )
        return True

    def _receipt(self, entry: Entry, response: httpx.Response) -> bool:
        if response.status_code not in (200, 201):
            return False
        try:
            receipt = Receipt.model_validate_json(response.content)
            manifest = Manifest.model_validate_json(entry.manifest or b"")
            if (
                receipt.capture_id != entry.capture_id
                or receipt.sha256 != manifest.sha256
            ):
                raise ValueError("receipt_mismatch")
        except (ValueError, ValidationError):
            with self._write() as db:
                db.execute(
                    "UPDATE entries SET state='quarantine', error='invalid_receipt' WHERE capture_id=?",
                    (entry.capture_id,),
                )
            self.notify("courier_quarantine")
            return True
        with self._write() as db:
            db.execute(
                "UPDATE entries SET state='received', receipt=?, error=NULL WHERE capture_id=?",
                (response.content, entry.capture_id),
            )
        # Separate durable commit: a crash here leaves a recoverable GC task.
        self.collect_garbage()
        return True

    def collect_garbage(self) -> None:
        with self._write() as db:
            db.execute(
                "UPDATE entries SET payload=NULL WHERE state='received' AND receipt IS NOT NULL"
            )

        self._compact()

    def _compact(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA incremental_vacuum").fetchall()

    def deliver(self, client: httpx.Client) -> None:
        """One bounded replay pass; caller schedules backoff and supplies auth/TLS.

        Client base_url ends in /mobile/v1/; never follow redirects. Supply a
        bearer from the private token file, not a browser session credential.
        """
        self.collect_garbage()
        if self.paused:
            return
        for entry in self.entries():
            if self.paused:
                break
            if entry.state != "pending" or (
                entry.depends_on
                and self.get(entry.depends_on, include_payload=False).receipt is None
            ):
                continue
            path = f"captures/{entry.capture_id}"
            try:
                response = client.get(path + "/receipt", follow_redirects=False)
                if self._refused(entry, response, receipt_probe=True) or self._receipt(
                    entry, response
                ):
                    continue
                if response.status_code != 404 and not self._payload_missing(response):
                    continue
                entry = self.get(entry.capture_id)
                assert entry.payload is not None and entry.manifest is not None
                manifest = Manifest.model_validate_json(entry.manifest)
                response = client.put(
                    path + "/payload",
                    content=entry.payload,
                    headers={
                        "Content-Length": str(len(entry.payload)),
                        "X-Content-SHA256": manifest.sha256,
                        "Content-Type": "application/octet-stream",
                    },
                    follow_redirects=False,
                )
                if self._refused(entry, response) or response.status_code not in (
                    200,
                    201,
                ):
                    continue
                response = client.put(
                    path + "/manifest",
                    content=entry.manifest,
                    headers={"Content-Type": "application/json"},
                    follow_redirects=False,
                )
                if not self._refused(entry, response):
                    self._receipt(entry, response)
            except httpx.RequestError:
                continue  # Preserve bytes; the next pass starts with Receipt GET.
