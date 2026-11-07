"""SourceStore: originals outside the vault, a SQLite index and receipts.

Layout under ``archive_root`` (never synced, never in the vault):

    index.sqlite3                      capture_id -> source_id, Source, manifests
    payloads/<sha[:2]>/<sha256>.<ext>  content-addressed originals

Commit order, each step durable before the next:

1. Stream the outbox body into a dot temporary next to its final name, checking
   size and sha256 again (the synced file may have changed since Courier
   verified it), fsync, then hard-link to the final name, which never replaces
   an existing file, and fsync the directory.
2. One ``BEGIN IMMEDIATE`` transaction inserts the Source (first manifest only)
   and the capture row (manifest verbatim and the exact receipt bytes).
3. Publish ``receipts/<capture_id>.json`` with the common atomic write.

A crash after 1 leaves an unindexed body that the retry reuses after hashing
it; a crash after 2 leaves an indexed capture whose receipt ``confirm``
re-publishes byte-identically. A receipt therefore only ever means "the body,
metadata and index are durable here", never that Spinner or Loom are done.
"""

import contextlib
import errno
import hashlib
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from weaver.config import ServerConfig
from weaver.courier.atomic import write_bytes_atomic
from weaver.courier.ports import (
    CaptureConflict,
    CaptureStatus,
    CaptureStore,
    VerifiedCapture,
)
from weaver.schema import Manifest, Receipt, Source
from weaver.schema.internal import ArchivedSource

from .ulid import new_source_id

INDEX_NAME = "index.sqlite3"
PAYLOADS = "payloads"
INDEX_VERSION = 1
# Own copy temporaries older than this are crash leftovers.
STALE_TEMPORARY_SECONDS = 600.0
_CHUNK = 1 << 20
_TEMPORARY = re.compile(r"^\.[0-9a-f]{64}\.[0-9a-f]{16}\.tmp$")
_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

_SCHEMA = (
    """CREATE TABLE sources (
        source_id TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE,
        payload_ref TEXT NOT NULL,
        source_json TEXT NOT NULL
    )""",
    """CREATE TABLE captures (
        seq INTEGER PRIMARY KEY,
        capture_id TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        sha256 TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        receipt_json TEXT NOT NULL
    )""",
    "CREATE INDEX captures_by_source ON captures(source_id, seq)",
)


class SourceStore(Protocol):
    def put(self, manifest: Manifest, payload: Path) -> Source:
        """Persist once, returning the existing Source for a duplicate."""
        ...

    def get(self, source_id: str) -> Source: ...


class ArchiveError(Exception):
    """Archive failure. Messages never quote payload or manifest content."""


class SourceNotFound(ArchiveError, LookupError):
    pass


class PayloadMismatch(ArchiveError):
    """The body to archive no longer matches the manifest size or sha256."""


class ArchiveIntegrityError(ArchiveError):
    """A file already at an archive path does not hold the expected bytes."""


@dataclass(frozen=True)
class CaptureRecord:
    """One manifest exactly as received, and the receipt published for it."""

    manifest: Manifest
    receipt: Receipt


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _write_chunk(fd: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        view = view[os.write(fd, view) :]


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _payload_ref(manifest: Manifest) -> str:
    _, dot, extension = manifest.payload.rpartition("/")[2].partition(".")
    suffix = f".{extension}" if dot else ""
    return f"{PAYLOADS}/{manifest.sha256[:2]}/{manifest.sha256}{suffix}"


def _hash_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class ArchiveStore:
    """SourceStore and Courier CaptureStore over one ``archive_root``.

    Safe for several threads and processes: every call opens its own SQLite
    connection and every index write runs under ``BEGIN IMMEDIATE``.
    """

    def __init__(
        self,
        archive_root: Path,
        receipts: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], str] = new_source_id,
    ) -> None:
        self.root = archive_root
        self.receipts = receipts
        self._clock = clock
        self._new_id = new_id
        self._index = archive_root / INDEX_NAME
        payloads = archive_root / PAYLOADS
        payloads.mkdir(mode=0o700, exist_ok=True)
        self._initialize_index()
        self.sweep_temporaries()

    @classmethod
    def from_config(
        cls,
        config: ServerConfig,
        *,
        clock: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], str] = new_source_id,
    ) -> "ArchiveStore":
        return cls(
            config.archive_root, config.courier.receipts, clock=clock, new_id=new_id
        )

    # ---- SourceStore ---------------------------------------------------------

    def put(self, manifest: Manifest, payload: Path) -> Source:
        """Archive once per body hash, index the capture, then publish its receipt.

        Same capture_id and sha256 returns the existing Source. A new capture_id
        with a stored body links to the existing Source. The same capture_id
        with another sha256 raises ``CaptureConflict`` and changes nothing.
        """
        source, receipt = self._persist(manifest, payload)
        self._publish_receipt(manifest.capture_id, receipt)
        return source

    def get(self, source_id: str) -> Source:
        with self._read() as conn:
            row = conn.execute(
                "SELECT source_json FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise SourceNotFound("unknown source_id")
        return Source.model_validate_json(row[0])

    # ---- Spinner-facing queries ---------------------------------------------

    def locate(self, source_id: str) -> ArchivedSource:
        """The Source and its local archived body, for Spinner to read."""
        source = self.get(source_id)
        return ArchivedSource(
            source=source, payload_path=self.root / source.payload_ref
        )

    def captures_for(self, source_id: str) -> list[CaptureRecord]:
        """Every manifest linked to this Source, in arrival order."""
        self.get(source_id)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT manifest_json, receipt_json FROM captures"
                " WHERE source_id = ? ORDER BY seq",
                (source_id,),
            ).fetchall()
        return [
            CaptureRecord(
                Manifest.model_validate_json(manifest),
                Receipt.model_validate_json(receipt),
            )
            for manifest, receipt in rows
        ]

    def source_for_capture(self, capture_id: str) -> str | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT source_id FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def sources(self) -> list[Source]:
        """Received Sources in arrival order. "Received" is not "processed":
        Spinner and Loom keep their own completion state."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT source_json FROM sources ORDER BY rowid"
            ).fetchall()
        return [Source.model_validate_json(row[0]) for row in rows]

    # ---- Courier CaptureStore ------------------------------------------------

    def status(self, capture_id: str, sha256: str) -> CaptureStatus:
        with self._read() as conn:
            row = conn.execute(
                "SELECT sha256 FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
        if row is None:
            return "new"
        return "stored" if row[0] == sha256 else "conflict"

    def confirm(self, capture_id: str) -> None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT receipt_json FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
        if row is None:
            raise SourceNotFound("unknown capture_id")
        self._publish_receipt(capture_id, row[0].encode("utf-8"))

    def accept(self, capture: VerifiedCapture) -> None:
        self.put(capture.manifest, capture.payload_path)

    # ---- maintenance ---------------------------------------------------------

    def sweep_temporaries(self) -> list[Path]:
        """Remove our own copy temporaries old enough that no write is in flight."""
        threshold = time.time() - STALE_TEMPORARY_SECONDS
        removed = []
        for directory in (self.root / PAYLOADS).iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                if not _TEMPORARY.fullmatch(path.name):
                    continue
                with contextlib.suppress(OSError):
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < threshold:
                        path.unlink()
                        removed.append(path)
        return removed

    # ---- internals -----------------------------------------------------------

    def _persist(self, manifest: Manifest, payload: Path) -> tuple[Source, bytes]:
        known = self._known_capture(manifest)
        if known is not None:
            return known
        with self._read() as conn:
            row = conn.execute(
                "SELECT payload_ref FROM sources WHERE sha256 = ?", (manifest.sha256,)
            ).fetchone()
        if row is None:
            payload_ref = _payload_ref(manifest)
            self._store_payload(manifest, payload, payload_ref)
        else:
            payload_ref = str(row[0])
            if not (self.root / payload_ref).is_file():
                raise ArchiveIntegrityError("indexed payload is missing")
        return self._index_capture(manifest, payload_ref)

    def _known_capture(self, manifest: Manifest) -> tuple[Source, bytes] | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT c.sha256, s.source_json, c.receipt_json FROM captures c"
                " JOIN sources s USING (source_id) WHERE c.capture_id = ?",
                (manifest.capture_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != manifest.sha256:
            raise CaptureConflict()
        return Source.model_validate_json(row[1]), row[2].encode("utf-8")

    def _store_payload(self, manifest: Manifest, payload: Path, ref: str) -> None:
        final = self.root / ref
        directory = final.parent
        if not directory.is_dir():
            directory.mkdir(mode=0o700, exist_ok=True)
            _fsync_directory(directory.parent)
        if final.exists():
            self._check_existing(final, manifest)
            return
        temporary = directory / f".{manifest.sha256}.{secrets.token_hex(8)}.tmp"
        try:
            source_fd = os.open(payload, _OPEN_FLAGS)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise PayloadMismatch("payload is a symlink") from None
            raise
        try:
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise PayloadMismatch("payload is not a regular file")
            target_fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
            )
            try:
                digest, size = hashlib.sha256(), 0
                while chunk := os.read(source_fd, _CHUNK):
                    size += len(chunk)
                    if size > manifest.size:
                        raise PayloadMismatch("payload larger than manifest")
                    digest.update(chunk)
                    _write_chunk(target_fd, chunk)
                if size != manifest.size or digest.hexdigest() != manifest.sha256:
                    raise PayloadMismatch("payload differs from manifest")
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
            try:
                os.link(temporary, final)  # never replaces an existing file
            except FileExistsError:
                self._check_existing(final, manifest)  # a concurrent identical put
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        finally:
            os.close(source_fd)
        temporary.unlink()
        _fsync_directory(directory)

    def _check_existing(self, final: Path, manifest: Manifest) -> None:
        if final.is_symlink() or not final.is_file():
            raise ArchiveIntegrityError("archive path is not a regular file")
        if _hash_file(final) != (manifest.sha256, manifest.size):
            raise ArchiveIntegrityError("archive path holds other bytes")

    def _index_capture(
        self, manifest: Manifest, payload_ref: str
    ) -> tuple[Source, bytes]:
        with self._write() as conn:
            row = conn.execute(
                "SELECT sha256, source_id, receipt_json FROM captures"
                " WHERE capture_id = ?",
                (manifest.capture_id,),
            ).fetchone()
            if row is not None:  # a concurrent put of the same capture won
                if row[0] != manifest.sha256:
                    raise CaptureConflict()
                source_json = conn.execute(
                    "SELECT source_json FROM sources WHERE source_id = ?", (row[1],)
                ).fetchone()[0]
                return Source.model_validate_json(source_json), row[2].encode("utf-8")
            existing = conn.execute(
                "SELECT source_json FROM sources WHERE sha256 = ?", (manifest.sha256,)
            ).fetchone()
            if existing is None:
                source = Source(
                    schema_version=1,
                    source_id=self._new_id(),
                    capture_id=manifest.capture_id,
                    kind=manifest.kind,
                    channel=manifest.channel,
                    captured_at=manifest.captured_at,
                    occurred_at=manifest.occurred_at,
                    sha256=manifest.sha256,
                    hints=manifest.hints,
                    payload_ref=payload_ref,
                )
                conn.execute(
                    "INSERT INTO sources (source_id, sha256, payload_ref, source_json)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        source.source_id,
                        source.sha256,
                        source.payload_ref,
                        source.model_dump_json(),
                    ),
                )
            else:
                source = Source.model_validate_json(existing[0])
            receipt = Receipt(
                schema_version=1,
                capture_id=manifest.capture_id,
                source_id=source.source_id,
                sha256=manifest.sha256,
                received_at=self._clock(),
            ).model_dump_json()
            conn.execute(
                "INSERT INTO captures"
                " (capture_id, source_id, sha256, manifest_json, receipt_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    manifest.capture_id,
                    source.source_id,
                    manifest.sha256,
                    manifest.model_dump_json(),
                    receipt,
                ),
            )
        return source, receipt.encode("utf-8")

    def _publish_receipt(self, capture_id: str, data: bytes) -> None:
        final = self.receipts / f"{capture_id}.json"
        with contextlib.suppress(OSError):
            if not final.is_symlink() and final.read_bytes() == data:
                return  # already published; avoid needless sync traffic
        write_bytes_atomic(final, data)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._index, timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = FULL")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(self._connect()) as conn:
            yield conn

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _initialize_index(self) -> None:
        created = not self._index.exists()
        with self._read() as conn:
            if conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise ArchiveError("index requires WAL journal mode")
        with self._write() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {INDEX_VERSION}")
            elif version != INDEX_VERSION:
                raise ArchiveError("unknown archive index version")
        if created:
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.chmod(f"{self._index}{suffix}", 0o600)


if TYPE_CHECKING:

    def _conforms(store: ArchiveStore) -> tuple[SourceStore, CaptureStore]:
        return store, store
