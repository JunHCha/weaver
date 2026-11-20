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

    def recover(self, state: SummaryState) -> list[str]:
        """Republish unfinished reviews, including legacy correction diffs.

        Completed files are intentionally cleaned by Courier; recovering them
        each tick would resurrect already consumed packages indefinitely.
        """
        if not self.enabled:
            return []
        with state.lock():
            return self._recover_locked(state)

    def _recover_locked(self, state: SummaryState) -> list[str]:
        with state.connect() as db:
            rows = db.execute(
                "SELECT p.package FROM packages p"
                " JOIN (SELECT id, MAX(revision) AS r FROM packages GROUP BY id) m"
                " ON p.id=m.id AND p.revision=m.r"
                " LEFT JOIN reviews v ON v.id=p.id AND v.revision=p.revision"
                " WHERE v.stage IS NULL OR v.stage!='APPLIED' ORDER BY p.id"
            ).fetchall()
            prepared = db.execute(
                "SELECT prepared FROM reviews WHERE stage='APPLIED'"
            ).fetchall()
        diffs: dict[tuple[str, int], str] = {}
        for (raw,) in prepared:
            value = json.loads(raw)
            if value.get("package") is not None:
                package = ReviewPackage.model_validate(value["package"])
                diffs[package.package_id, package.revision] = value.get("diff", "")
        written = []
        for (raw,) in rows:
            package = ReviewPackage.model_validate_json(raw)
            if self.publish(
                package, diff=diffs.get((package.package_id, package.revision))
            ):
                written.append(f"{package.package_id}.r{package.revision}.json")
        return written


def _matches(path: Path, data: bytes) -> bool:
    return not path.is_symlink() and path.is_file() and path.read_bytes() == data
