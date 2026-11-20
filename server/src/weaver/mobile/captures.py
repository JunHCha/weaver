"""capture upload: payload, then manifest, then a durable Receipt.

A payload PUT only stages bytes in private storage; it is not a Receipt. The
manifest PUT hands the staged body to the same ``ArchiveStore`` the file path
uses, so its capture/sha rules (and its Receipt) are the shared journal.
"""

import hashlib
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import ValidationError

from weaver.archive import ArchiveStore, PayloadMismatch
from weaver.courier.atomic import fsync_directory
from weaver.courier.ports import CaptureConflict
from weaver.schema import Manifest, Receipt
from weaver.schema.models import ULID

from .errors import ApiError
from .store import MobileStore

logger = logging.getLogger("weaver.mobile")
CAPTURE_ID = re.compile(rf"^cap_{ULID}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 64 * 1024
_CHUNK = 1 << 20


class CaptureService:
    def __init__(
        self,
        archive: ArchiveStore,
        store: MobileStore,
        staging: Path,
        *,
        max_payload_bytes: int,
        staging_days: int,
    ) -> None:
        self.archive = archive
        self.store = store
        self.staging = staging
        self.max_payload_bytes = max_payload_bytes
        self.staging_days = staging_days
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(staging, 0o700)

    def _path(self, capture_id: str) -> Path:
        return self.staging / f"{capture_id}.bin"

    @staticmethod
    def check_id(capture_id: str) -> None:
        if not CAPTURE_ID.fullmatch(capture_id):
            raise ApiError(422, "invalid_capture_id")

    async def put_payload(
        self,
        capture_id: str,
        device_id: str,
        declared: int | None,
        sha256: str | None,
        body: AsyncIterator[bytes],
    ) -> tuple[int, dict[str, object]]:
        self.check_id(capture_id)
        if declared is None:
            raise ApiError(411, "length_required")
        if sha256 is None or not SHA256.fullmatch(sha256):
            raise ApiError(422, "invalid_sha256")
        if declared > self.max_payload_bytes:
            raise ApiError(413, "too_large")
        stored = self.archive.stored_capture(capture_id)
        if stored is not None:
            if stored[1].sha256 != sha256:
                raise ApiError(409, "capture_conflict")
            return 200, {"capture_id": capture_id, "state": "stored"}
        staged = self.store.staged(capture_id)
        if staged is not None and self._path(capture_id).is_file():
            if staged[1] != sha256:
                raise ApiError(409, "payload_conflict")
            return 200, {"capture_id": capture_id, "state": "staged"}
        temporary = self.staging / f".{capture_id}.{os.urandom(8).hex()}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as out:
                async for chunk in body:
                    size += len(chunk)
                    # Bound by what is actually read, not by the header alone.
                    if size > declared or size > self.max_payload_bytes:
                        raise ApiError(400, "length_mismatch")
                    digest.update(chunk)
                    out.write(chunk)
                if size != declared:
                    raise ApiError(400, "length_mismatch", retryable=True)
                if digest.hexdigest() != sha256:
                    raise ApiError(422, "sha256_mismatch")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self._path(capture_id))
            fsync_directory(self.staging)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.store.stage(capture_id, device_id, sha256, size)
        return 201, {"capture_id": capture_id, "state": "staged"}

    def put_manifest(self, capture_id: str, raw: bytes) -> tuple[int, Receipt]:
        self.check_id(capture_id)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ApiError(413, "too_large")
        try:
            manifest = Manifest.model_validate_json(raw)
        except ValidationError:
            raise ApiError(422, "invalid_manifest") from None
        if manifest.capture_id != capture_id:
            raise ApiError(422, "id_mismatch")
        stem, dot, extension = manifest.payload.partition(".")
        if (
            "/" in manifest.payload
            or stem != capture_id
            or not dot
            or extension == "json"
        ):
            raise ApiError(422, "invalid_payload")
        known = self.archive.stored_capture(capture_id)
        if known is not None:
            original, receipt = known
            if original.sha256 != manifest.sha256:
                raise ApiError(409, "capture_conflict")
            if original != manifest:
                raise ApiError(409, "manifest_conflict")
            self._release(capture_id)
            return 200, receipt
        # Checked after the idempotent replay: a call stored before this policy keeps its 200.
        if manifest.channel == "call" and manifest.occurred_at is None:
            raise ApiError(422, "call_occurred_at_required")
        path = self._path(capture_id)
        staged = self.store.staged(capture_id)
        if staged is None or not path.is_file():
            if not self.archive.has_body(manifest.sha256):
                raise ApiError(409, "payload_missing", retryable=True)
        elif (staged[1], staged[2]) != (manifest.sha256, manifest.size):
            raise ApiError(409, "payload_mismatch")
        try:
            self.archive.put(manifest, path)
        except CaptureConflict:
            raise ApiError(409, "capture_conflict") from None
        except PayloadMismatch:
            raise ApiError(422, "payload_mismatch") from None
        except OSError:
            logger.error("capture commit failed: io_error")
            raise ApiError(503, "storage_unavailable", retryable=True) from None
        stored = self.archive.stored_capture(capture_id)
        assert stored is not None
        self._release(capture_id)
        return 201, stored[1]

    def receipt(self, capture_id: str) -> Receipt:
        self.check_id(capture_id)
        stored = self.archive.stored_capture(capture_id)
        if stored is None:
            raise ApiError(404, "receipt_not_found")
        return stored[1]

    def _release(self, capture_id: str) -> None:
        """The archive owns the body now; staged copies are no longer needed."""
        self.store.unstage(capture_id)
        self._path(capture_id).unlink(missing_ok=True)

    def collect_garbage(self, now: float | None = None) -> list[str]:
        """Drop uncommitted staging older than ``staging_days``; archives stay."""
        from datetime import UTC, datetime, timedelta

        cutoff = (
            datetime.fromtimestamp(now or time.time(), UTC)
            - timedelta(days=self.staging_days)
        ).isoformat(timespec="microseconds")
        removed = []
        for capture_id in self.store.expired_staged(cutoff):
            if self.archive.stored_capture(capture_id) is None:
                self._release(capture_id)
                removed.append(capture_id)
        return removed
