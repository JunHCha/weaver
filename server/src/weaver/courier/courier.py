"""Courier file protocol on the server side.

Every scan re-reads the four folders from scratch, so files that arrived while
the server was down, or that synced in a different order, are picked up on the
next pass. A file that cannot be processed yet is left in place and retried on
every scan, except bounded application retries. Logs carry only the folder,
file name and an error code.
"""

import hashlib
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from weaver.config import CourierPaths, CourierSettings
from weaver.schema import Alert, Manifest, ReviewPackage, Verdict

from .atomic import write_json_atomic
from .files import (
    ALERT_NAME,
    MANIFEST_NAME,
    CourierFileError,
    is_conflict,
    is_ignored,
    is_own_temporary,
    load_versioned,
    open_regular,
    parse_revision_name,
)
from .ports import (
    CaptureConflict,
    CaptureStore,
    DecisionStore,
    PackageRevisions,
    VerifiedCapture,
)

logger = logging.getLogger("weaver.courier")

SCAN_INTERVAL_SECONDS = 5.0
# Own atomic-write temporaries older than this are crash leftovers.
STALE_TEMPORARY_SECONDS = 600.0
_HASH_CHUNK = 1 << 20
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")

# Codes for conditions that normally resolve on their own (late sync arrival).
_PENDING = {
    "apply_waiting",
    "apply_backoff",
    "missing",
    "payload_missing",
    "payload_changed",
    "size_mismatch",
    "sha256_mismatch",
}
# Codes that indicate forged, malformed or conflicting input.
_WARNING = {
    "apply_stopped",
    "id_mismatch",
    "rev_mismatch",
    "invalid_json",
    "invalid_schema",
    "invalid_payload",
    "payload_outside",
    "symlink",
    "not_regular_file",
    "too_large",
    "unknown_version",
    "stale_rev",
    "unknown_rev",
    "unknown_package",
    "sync_conflict",
    "unreadable",
    "capture_conflict",
    "verdict_conflict",
}


def _ask[**P, T](query: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run an injected store query; its failure is a per-file, retryable code."""
    try:
        return query(*args, **kwargs)
    except Exception as exc:
        # Only validated opaque IDs/revisions are supplied by these query ports.
        safe_args = tuple(
            a
            for a in args
            if isinstance(a, int) or (isinstance(a, str) and SAFE_ID.fullmatch(a))
        )
        logger.warning("courier query failed: %s %s", type(exc).__name__, safe_args)
        raise CourierFileError("store_failed") from None


@dataclass
class ScanReport:
    accepted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    applied: list[tuple[str, int]] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    swept: list[str] = field(default_factory=list)
    problems: dict[str, str] = field(default_factory=dict)


@dataclass
class ApplyFailure:
    verdict_digest: str
    error_digest: str
    attempts: int
    next_attempt: float


class Courier:
    def __init__(
        self,
        paths: CourierPaths,
        captures: CaptureStore,
        revisions: PackageRevisions,
        decisions: DecisionStore,
        *,
        retry: CourierSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        can_apply: Callable[[], bool] | None = None,
    ) -> None:
        self._can_apply = can_apply
        self._retry = retry or CourierSettings()
        self._clock = clock
        # Deliberately volatile: restarting after a code/config fix must retry
        # existing decisions. No raw verdict or exception text is retained.
        self._apply_failures: dict[str, ApplyFailure] = {}
        self.paths = paths
        self._captures = captures
        self._revisions = revisions
        self._decisions = decisions
        self._reported: dict[tuple[str, str], str] = {}

    # ---- publishing (PC -> phone) -------------------------------------------

    def publish_alert(self, alert: Alert) -> Path:
        final = self.paths.proposals / f"alert_{alert.alert_id}.json"
        write_json_atomic(final, alert)
        return final

    # ---- scanning (phone -> PC) ---------------------------------------------

    def scan_once(self) -> ScanReport:
        report = ScanReport()
        current: dict[tuple[str, str], str] = {}
        for folder, scan in (
            ("outbox", self._scan_outbox),
            ("proposals", self._scan_packages),
            ("decisions", self._scan_decisions),
            ("receipts", self._sweep_receipts),
        ):
            try:
                scan(report, current)
            except OSError:
                self._report(current, folder, "", "folder_unreadable", report)
        self._reported = current
        return report

    def run(
        self, stop: threading.Event, interval: float = SCAN_INTERVAL_SECONDS
    ) -> None:
        """Rescan every ``interval`` seconds until ``stop`` is set."""
        while not stop.is_set():
            try:
                self.scan_once()
            except Exception as error:
                # Type only: messages and tracebacks may quote file contents.
                logger.error("courier scan failed: %s", type(error).__name__)
            stop.wait(interval)

    def _entries(
        self,
        folder: str,
        directory: Path,
        report: ScanReport,
        current: dict[tuple[str, str], str],
    ) -> Iterator[Path]:
        for path in sorted(directory.iterdir()):
            name = path.name
            if is_ignored(name):
                continue
            if is_conflict(name):
                self._report(current, folder, name, "sync_conflict", report)
                continue
            yield path

    def _scan_outbox(
        self, report: ScanReport, current: dict[tuple[str, str], str]
    ) -> None:
        for path in self._entries("outbox", self.paths.outbox, report, current):
            match = MANIFEST_NAME.fullmatch(path.name)
            if match is None:
                continue  # Payload bodies and foreign files are not manifests.
            try:
                manifest = load_versioned(path, Manifest, version=1)
                if manifest.capture_id != match.group(1):
                    raise CourierFileError("id_mismatch")
                # Before touching the body: a completed capture is never
                # re-hashed, and the phone may already have removed its body.
                status = _ask(
                    self._captures.status, manifest.capture_id, manifest.sha256
                )
                if status == "conflict":
                    raise CourierFileError("capture_conflict")
                if status == "stored":
                    # Recovers a receipt lost between persist and publish.
                    _ask(self._captures.confirm, manifest.capture_id)
                    report.duplicates.append(manifest.capture_id)
                    continue
                capture = self._verify_capture(path, manifest)
                try:
                    self._captures.accept(capture)
                except CaptureConflict:
                    raise CourierFileError("capture_conflict") from None
                except Exception:
                    raise CourierFileError("accept_failed") from None
                report.accepted.append(manifest.capture_id)
            except CourierFileError as error:
                self._report(current, "outbox", path.name, error.code, report)
            except OSError:
                self._report(current, "outbox", path.name, "io_error", report)

    def _verify_capture(self, path: Path, manifest: Manifest) -> VerifiedCapture:
        payload = manifest.payload
        # The schema already excludes absolute paths and "..". Courier further
        # requires a sibling body named <capture_id>.<ext>.
        stem, dot, extension = payload.partition(".")
        if (
            "/" in payload
            or stem != manifest.capture_id
            or not dot
            or extension == "json"
        ):
            raise CourierFileError("invalid_payload")
        root = self.paths.outbox.resolve()
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            name = payload
            try:
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    # A link may only point at a sibling regular file in outbox.
                    target = (self.paths.outbox / payload).resolve()
                    if target.parent != root:
                        raise CourierFileError("payload_outside")
                    name = target.name
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise CourierFileError("payload_missing") from None
            except (OSError, RuntimeError):
                raise CourierFileError("unreadable") from None
            try:
                fd = open_regular(name, dir_fd=root_fd)
            except CourierFileError as error:
                if error.code == "missing":
                    raise CourierFileError("payload_missing") from None
                raise
        finally:
            os.close(root_fd)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise CourierFileError("payload_changed")
            digest = hashlib.sha256()
            size = 0
            try:
                while chunk := stream.read(_HASH_CHUNK):
                    size += len(chunk)
                    if size > manifest.size:
                        raise CourierFileError("size_mismatch")
                    digest.update(chunk)
            except OSError:
                raise CourierFileError("unreadable") from None
        if size != manifest.size:
            raise CourierFileError("size_mismatch")
        if digest.hexdigest() != manifest.sha256:
            raise CourierFileError("sha256_mismatch")
        return VerifiedCapture(manifest, path, root / name)

    def _scan_packages(
        self, report: ScanReport, current: dict[tuple[str, str], str]
    ) -> None:
        """Drop superseded or already-decided revs that reappear after resync."""
        self._sweep_temporaries("proposals", self.paths.proposals, report, current)
        for path in self._entries("proposals", self.paths.proposals, report, current):
            if ALERT_NAME.fullmatch(path.name):
                continue  # Alerts are consumed and removed by the phone.
            name = parse_revision_name(path.name)
            if name is None:
                continue
            try:
                package = load_versioned(path, ReviewPackage, version=2)
                if package.package_id != name.package_id:
                    raise CourierFileError("id_mismatch")
                if package.revision != name.rev:
                    raise CourierFileError("rev_mismatch")
                pid, rev = name.package_id, name.rev
                latest = _ask(self._revisions.latest_rev, pid)
                if latest is None:
                    raise CourierFileError("unknown_package")
                if rev > latest:
                    raise CourierFileError("unknown_rev")
                if rev < latest or _ask(self._decisions.is_applied, pid, rev):
                    path.unlink(missing_ok=True)
                    report.pruned.append(path.name)
            except CourierFileError as error:
                self._report(current, "proposals", path.name, error.code, report)
            except OSError:
                self._report(current, "proposals", path.name, "io_error", report)

    def _scan_decisions(
        self, report: ScanReport, current: dict[tuple[str, str], str]
    ) -> None:
        present: set[str] = set()
        for path in self._entries("decisions", self.paths.decisions, report, current):
            present.add(path.name)
            name = parse_revision_name(path.name)
            if name is None:
                continue
            try:
                verdict = load_versioned(path, Verdict, version=2)
                if verdict.package_id != name.package_id:
                    raise CourierFileError("id_mismatch")
                if verdict.revision != name.rev:
                    raise CourierFileError("rev_mismatch")
                pid, rev = name.package_id, name.rev
                # Loom's optional guard checks conflicting resynced decisions
                # before the applied-file cleanup shortcut.
                validate = getattr(self._decisions, "validate_verdict", None)
                if validate is not None:
                    try:
                        validate(verdict)
                    except CourierFileError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "courier recovery failed: %s (%s, %s)",
                            type(exc).__name__,
                            pid,
                            rev,
                        )
                        raise CourierFileError("store_failed") from None
                if _ask(self._decisions.is_applied, pid, rev):
                    self._clean_decision(path, pid, rev, report)
                    continue
                latest = _ask(self._revisions.latest_rev, pid)
                if latest is None:
                    raise CourierFileError("unknown_package")
                if rev < latest:
                    raise CourierFileError("stale_rev")
                if rev > latest:
                    raise CourierFileError("unknown_rev")
                if self._can_apply is not None and not self._can_apply():
                    raise CourierFileError("apply_waiting")
                digest = hashlib.sha256(verdict.model_dump_json().encode()).hexdigest()
                failure = self._apply_failures.get(path.name)
                if failure is not None and failure.verdict_digest != digest:
                    self._apply_failures.pop(path.name)
                    failure = None
                if failure is not None:
                    if failure.attempts >= self._retry.max_attempts:
                        raise CourierFileError("apply_stopped")
                    if self._clock() < failure.next_attempt:
                        raise CourierFileError("apply_backoff")
                try:
                    self._decisions.apply(verdict)
                    if not _ask(self._decisions.is_applied, pid, rev):
                        raise CourierFileError("apply_unconfirmed")
                except Exception as exc:
                    logger.warning(
                        "courier apply failed: %s (%s, %s)",
                        type(exc).__name__,
                        pid,
                        rev,
                    )
                    self._record_apply_failure(path.name, digest, exc)
                    code = (
                        exc.code
                        if isinstance(exc, CourierFileError)
                        else "apply_failed"
                    )
                    raise CourierFileError(code) from None
                self._apply_failures.pop(path.name, None)
                report.applied.append((pid, rev))
                self._clean_decision(path, pid, rev, report)
            except CourierFileError as error:
                self._report(current, "decisions", path.name, error.code, report)
            except OSError:
                self._report(current, "decisions", path.name, "io_error", report)

        self._apply_failures = {
            name: failure
            for name, failure in self._apply_failures.items()
            if name in present
        }

    def _record_apply_failure(
        self, name: str, verdict_digest: str, exc: Exception
    ) -> None:
        error_digest = hashlib.sha256(
            f"{type(exc).__module__}.{type(exc).__qualname__}:{exc}".encode()
        ).hexdigest()
        previous = self._apply_failures.get(name)
        attempts = (
            previous.attempts + 1
            if previous is not None and previous.error_digest == error_digest
            else 1
        )
        # Iterative multiplication avoids huge powers for user-supplied limits.
        delay = self._retry.backoff_seconds
        for _ in range(attempts - 1):
            delay = min(delay * 2, self._retry.max_backoff_seconds)
            if delay == self._retry.max_backoff_seconds:
                break
        self._apply_failures[name] = ApplyFailure(
            verdict_digest, error_digest, attempts, self._clock() + delay
        )
        if attempts == self._retry.max_attempts:
            logger.warning(
                "courier apply retries stopped: %s after %s attempts; "
                "restart or submit a new verdict to retry",
                name,
                attempts,
            )

    def _sweep_receipts(
        self, report: ScanReport, current: dict[tuple[str, str], str]
    ) -> None:
        self._sweep_temporaries("receipts", self.paths.receipts, report, current)

    def _sweep_temporaries(
        self,
        folder: str,
        directory: Path,
        report: ScanReport,
        current: dict[tuple[str, str], str],
    ) -> None:
        """Remove our own crash-leftover temporaries so they stop syncing.

        Only server-published folders, only our exact name pattern, only
        regular files, and only once old enough that no write is in flight.
        """
        threshold = time.time() - STALE_TEMPORARY_SECONDS
        for path in directory.iterdir():
            if not is_own_temporary(path.name):
                continue
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_mtime < threshold:
                    path.unlink(missing_ok=True)
                    report.swept.append(f"{folder}/{path.name}")
            except OSError:
                self._report(current, folder, path.name, "io_error", report)

    def _clean_decision(
        self, path: Path, package_id: str, rev: int, report: ScanReport
    ) -> None:
        # Package first: if we stop in between, the remaining decision makes
        # the next scan repeat this idempotent cleanup.
        self._remove_revisions(package_id, below=rev + 1)
        path.unlink(missing_ok=True)
        self._apply_failures.pop(path.name, None)
        report.cleaned.append(path.name)

    def _remove_revisions(self, package_id: str, below: int) -> None:
        for path in self.paths.proposals.iterdir():
            name = parse_revision_name(path.name)
            if name is not None and name.package_id == package_id:
                if name.rev < below:
                    path.unlink(missing_ok=True)

    def _report(
        self,
        current: dict[tuple[str, str], str],
        folder: str,
        name: str,
        code: str,
        report: ScanReport,
    ) -> None:
        key = (folder, name)
        current[key] = code
        report.problems[f"{folder}/{name}"] = code
        if self._reported.get(key) == code:
            level = logging.DEBUG  # Still retried; avoid repeating every scan.
        elif code in _PENDING:
            level = logging.INFO
        elif code in _WARNING:
            level = logging.WARNING
        else:
            level = logging.ERROR
        logger.log(level, "courier %s/%s: %s", folder, name, code)
