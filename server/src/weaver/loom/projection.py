"""Optional shared-folder review projection; SummaryState remains authoritative."""

import json
import logging
from pathlib import Path

from weaver.courier.atomic import write_bytes_atomic, write_json_atomic
from weaver.loom.summary_state import SummaryState
from weaver.schema import ReviewPackage

logger = logging.getLogger(__name__)


class ReviewProjection:
    """A recoverable file adapter, never part of the domain commit."""

    def __init__(
        self, directory: Path, *, enabled: bool = True, strict: bool = False
    ) -> None:
        self.directory = directory
        self.enabled = enabled
        self.strict = strict

    def publish(self, package: ReviewPackage, *, diff: str | None = None) -> bool:
        if not self.enabled:
            return False
        changed = False
        try:
            if self.directory.is_symlink():
                raise OSError("review projection directory is a symlink")
            self.directory.mkdir(parents=True, exist_ok=True)
            stem = f"{package.package_id}.r{package.revision}"
            if diff is not None:
                target = self.directory / f"{stem}.diff"
                if not _matches(target, diff.encode()):
                    write_bytes_atomic(target, diff.encode())
                    changed = True
            target = self.directory / f"{stem}.json"
            if not _matches(target, package.model_dump_json().encode()):
                write_json_atomic(target, package)
                changed = True
        except OSError as error:
            if self.strict:
                raise
            logger.warning("review projection unavailable: %s", type(error).__name__)
            return False
        return changed

    def _retract(self, package_id: str) -> None:
        """Remove a superseded review's files so Courier stops offering them."""
        try:
            for path in self.directory.glob(f"{package_id}.r*"):
                if path.suffix in (".json", ".diff") and not path.is_symlink():
                    path.unlink(missing_ok=True)
        except OSError as error:
            if self.strict:
                raise
            logger.warning("review projection unavailable: %s", type(error).__name__)

    def recover(self, state: SummaryState) -> list[str]:
        """Publish undecided reviews and retry completed revisions exactly until success."""
        if not self.enabled:
            return []
        # lock() is not reentrant. Snapshot only; no shared-folder I/O under it.
        with state.lock(), state.connect() as db:
            rows = db.execute(
                "SELECT p.package, previous.prepared FROM packages p"
                " JOIN (SELECT id, MAX(revision) AS r FROM packages GROUP BY id) m"
                " ON p.id=m.id AND p.revision=m.r"
                " LEFT JOIN reviews v ON v.id=p.id AND v.revision=p.revision"
                " LEFT JOIN reviews previous ON previous.id=p.id"
                " AND previous.revision=p.revision-1 AND previous.stage='APPLIED'"
                " WHERE v.verdict IS NULL ORDER BY p.id"
            ).fetchall()
            completed = db.execute(
                "SELECT package FROM pending_projections ORDER BY id, revision"
            ).fetchall()
        superseded = state.superseded_sources()
        publications: list[tuple[ReviewPackage, str | None, bool]] = []
        for raw, prepared in rows:
            package = ReviewPackage.model_validate_json(raw)
            if package.source_id in superseded:
                self._retract(package.package_id)
                continue
            diff = None
            if prepared is not None:
                value = json.loads(prepared)
                if value.get("package") is not None:
                    diff = value.get("diff", "")
            publications.append((package, diff, False))
        publications.extend(
            (ReviewPackage.model_validate_json(raw), None, True) for (raw,) in completed
        )
        written = []
        writer = ReviewProjection(self.directory, strict=True)
        for package, diff, pending in publications:
            try:
                changed = writer.publish(package, diff=diff)
            except OSError as error:
                if self.strict:
                    raise
                logger.warning(
                    "review projection unavailable: %s", type(error).__name__
                )
                continue
            if pending:
                # A matching file is also a successful publication. Keep the marker
                # on failure/crash, but never resurrect consumed files on later ticks.
                with state.connect() as db:
                    db.execute(
                        "DELETE FROM pending_projections WHERE id=? AND revision=?",
                        (package.package_id, package.revision),
                    )
            if changed:
                written.append(f"{package.package_id}.r{package.revision}.json")
        return written


def _matches(path: Path, data: bytes) -> bool:
    return not path.is_symlink() and path.is_file() and path.read_bytes() == data
