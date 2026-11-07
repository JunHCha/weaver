"""Courier file protocol v1 on the server side.

Every scan re-reads the four folders from scratch, so files that arrived while
the server was down, or that synced in a different order, are picked up on the
next pass. A file that cannot be processed yet is left in place and retried on
every scan. Logs carry only the folder, file name and an error code.
"""

import hashlib
import logging
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from weaver.config import CourierPaths
from weaver.schema import Alert, Manifest, Proposal, Verdict

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
    revision_file_name,
)
from .ports import CaptureStore, DecisionStore, ProposalRevisions, VerifiedCapture

logger = logging.getLogger("weaver.courier")

SCAN_INTERVAL_SECONDS = 5.0
# Own atomic-write temporaries older than this are crash leftovers.
STALE_TEMPORARY_SECONDS = 600.0
_HASH_CHUNK = 1 << 20

# Codes for conditions that normally resolve on their own (late sync arrival).
_PENDING = {
    "missing",
    "payload_missing",
    "payload_changed",
    "size_mismatch",
    "sha256_mismatch",
}
# Codes that indicate forged, malformed or conflicting input.
_WARNING = {
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
    "unknown_proposal",
    "sync_conflict",
    "unreadable",
}


class RevisionError(ValueError):
    """Refused to publish a rev that is not the durably recorded latest rev."""


def _ask[**P, T](query: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run an injected store query; its failure is a per-file, retryable code."""
    try:
        return query(*args, **kwargs)
    except Exception:
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


class Courier:
    def __init__(
        self,
        paths: CourierPaths,
        captures: CaptureStore,
        revisions: ProposalRevisions,
        decisions: DecisionStore,
    ) -> None:
        self.paths = paths
        self._captures = captures
        self._revisions = revisions
        self._decisions = decisions
        self._reported: dict[tuple[str, str], str] = {}

    # ---- publishing (PC -> phone) -------------------------------------------

    def publish_proposal(self, proposal: Proposal) -> Path:
        """Write ``<proposal_id>.r<rev>.json`` atomically, then drop older revs.

        Loom must record ``proposal.rev`` durably before calling this.
        """
        latest = self._revisions.latest_rev(proposal.proposal_id)
        if latest is None or proposal.rev != latest:
            raise RevisionError("proposal rev is not the recorded latest rev")
        final = self.paths.proposals / revision_file_name(
            proposal.proposal_id, proposal.rev
        )
        write_json_atomic(final, proposal)
        self._remove_revisions(proposal.proposal_id, below=proposal.rev)
        return final

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
            ("proposals", self._scan_proposals),
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
                manifest = load_versioned(path, Manifest)
                if manifest.capture_id != match.group(1):
                    raise CourierFileError("id_mismatch")
                # Before touching the body: a completed capture is never
                # re-hashed, and the phone may already have removed its body.
                if _ask(
                    self._captures.is_captured, manifest.capture_id, manifest.sha256
                ):
                    report.duplicates.append(manifest.capture_id)
                    continue
                capture = self._verify_capture(path, manifest)
                try:
                    self._captures.accept(capture)
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

    def _scan_proposals(
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
                proposal = load_versioned(path, Proposal)
                if proposal.proposal_id != name.proposal_id:
                    raise CourierFileError("id_mismatch")
                if proposal.rev != name.rev:
                    raise CourierFileError("rev_mismatch")
                pid, rev = name.proposal_id, name.rev
                latest = _ask(self._revisions.latest_rev, pid)
                if latest is None:
                    raise CourierFileError("unknown_proposal")
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
        for path in self._entries("decisions", self.paths.decisions, report, current):
            name = parse_revision_name(path.name)
            if name is None:
                continue
            try:
                verdict = load_versioned(path, Verdict)
                if verdict.proposal_id != name.proposal_id:
                    raise CourierFileError("id_mismatch")
                if verdict.rev != name.rev:
                    raise CourierFileError("rev_mismatch")
                pid, rev = name.proposal_id, name.rev
                if _ask(self._decisions.is_applied, pid, rev):
                    self._clean_decision(path, pid, rev, report)
                    continue
                latest = _ask(self._revisions.latest_rev, pid)
                if latest is None:
                    raise CourierFileError("unknown_proposal")
                if rev < latest:
                    raise CourierFileError("stale_rev")
                if rev > latest:
                    raise CourierFileError("unknown_rev")
                try:
                    self._decisions.apply(verdict)
                except Exception:
                    raise CourierFileError("apply_failed") from None
                if not _ask(self._decisions.is_applied, pid, rev):
                    raise CourierFileError("apply_unconfirmed")
                report.applied.append((pid, rev))
                self._clean_decision(path, pid, rev, report)
            except CourierFileError as error:
                self._report(current, "decisions", path.name, error.code, report)
            except OSError:
                self._report(current, "decisions", path.name, "io_error", report)

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
        self, path: Path, proposal_id: str, rev: int, report: ScanReport
    ) -> None:
        # Proposal first: if we stop in between, the remaining decision makes
        # the next scan repeat this idempotent cleanup.
        self._remove_revisions(proposal_id, below=rev + 1)
        path.unlink(missing_ok=True)
        report.cleaned.append(path.name)

    def _remove_revisions(self, proposal_id: str, below: int) -> None:
        for path in self.paths.proposals.iterdir():
            name = parse_revision_name(path.name)
            if name is not None and name.proposal_id == proposal_id:
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
