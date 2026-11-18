"""Review packages and Verdicts over HTTP (journal: ``reviews`` + operations)."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from weaver.courier.atomic import write_json_atomic
from weaver.loom.summary_state import SummaryState
from weaver.schema import ReviewPackage, Verdict

from .errors import ApiError
from .store import MobileStore, Operation, canonical, content_hash


def etag(package: ReviewPackage) -> str:
    return '"' + hashlib.sha256(package.model_dump_json().encode()).hexdigest() + '"'


@dataclass(frozen=True)
class ReviewEntry:
    package: ReviewPackage

    @property
    def summary(self) -> dict[str, object]:
        p = self.package
        return {
            "package_id": p.package_id,
            "revision": p.revision,
            "source_id": p.source_id,
            "title": p.title,
            "source_type": p.source_type,
            "stage": p.stage,
            "etag": etag(p),
        }


class ReviewService:
    def __init__(self, state: SummaryState, store: MobileStore) -> None:
        self.state = state
        self.store = store

    def open_packages(self) -> list[ReviewPackage]:
        """Latest revision of each package whose review is not yet applied."""
        with self.state.connect() as db:
            rows = db.execute(
                "SELECT p.package FROM packages p"
                " JOIN (SELECT id, MAX(revision) AS r FROM packages GROUP BY id) m"
                "   ON p.id = m.id AND p.revision = m.r"
                " LEFT JOIN reviews v ON v.id = p.id AND v.revision = p.revision"
                " WHERE v.stage IS NULL OR v.stage != 'APPLIED' ORDER BY p.id"
            ).fetchall()
        return [ReviewPackage.model_validate_json(row[0]) for row in rows]

    def latest_rev(self, package_id: str) -> int | None:
        with self.state.connect() as db:
            row = db.execute(
                "SELECT MAX(revision) FROM packages WHERE id=?", (package_id,)
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def package(self, package_id: str, revision: int) -> ReviewPackage:
        try:
            return self.state.load_package(package_id, revision)[0]
        except ValueError:
            raise ApiError(404, "package_not_found") from None

    def recorded_verdict(self, package_id: str, revision: int) -> Verdict | None:
        with self.state.connect() as db:
            row = db.execute(
                "SELECT verdict FROM reviews WHERE id=? AND revision=?",
                (package_id, revision),
            ).fetchone()
        return None if row is None else Verdict.model_validate(json.loads(row[0]))

    def submit(
        self,
        device_id: str,
        package_id: str,
        revision: int,
        if_match: str | None,
        raw: bytes,
    ) -> tuple[Operation, bool]:
        """Journal one Verdict; the resident runtime applies it later.

        Same ``(package_id, revision)`` and content returns the same operation
        (retry after a lost 202); other content is a conflict.
        """
        if len(raw) > 1 << 20:
            raise ApiError(413, "too_large")
        try:
            verdict = Verdict.model_validate_json(raw)
        except ValidationError:
            raise ApiError(422, "invalid_verdict") from None
        if (verdict.package_id, verdict.revision) != (package_id, revision):
            raise ApiError(422, "id_mismatch")
        key = f"{package_id}:{revision}"
        digest = content_hash(canonical(verdict.model_dump(mode="json")))
        existing = self.store.find_operation("verdict", key)
        if existing is not None:
            if existing.content_hash != digest:
                raise ApiError(409, "verdict_conflict")
            return existing, False
        latest = self.latest_rev(package_id)
        if latest is None or revision > latest:
            raise ApiError(404, "package_not_found")
        if revision < latest:
            raise ApiError(412, "stale_revision", extra={"current_revision": latest})
        package = self.package(package_id, revision)
        if if_match is None:
            raise ApiError(428, "if_match_required")
        if if_match != etag(package):
            raise ApiError(412, "etag_mismatch", extra={"current_revision": latest})
        recorded = self.recorded_verdict(package_id, revision)
        if recorded is not None and recorded != verdict:
            # A different decision already arrived (for example over files).
            raise ApiError(409, "verdict_conflict")
        return self.store.enqueue(
            "verdict",
            device_id,
            key,
            digest,
            package_id,
            revision,
            verdict.model_dump_json(),
        )

    def republish(self, proposals: Path) -> list[str]:
        """Write unfinished packages back to ``proposals/`` (return to files)."""
        written = []
        for package in self.open_packages():
            target = proposals / f"{package.package_id}.r{package.revision}.json"
            data = package.model_dump_json().encode()
            if not target.is_file() or _normal(target) != _normal_bytes(data):
                write_json_atomic(target, package)
                written.append(target.name)
        return written


def _normal_bytes(data: bytes) -> object:
    return json.loads(data)


def _normal(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except ValueError:
        return None
