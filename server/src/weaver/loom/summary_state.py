"""Durable journal for summary drafts and review side effects."""

import fcntl
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from weaver.readonly import connect_snapshot
from weaver.schema import ReviewPackage
from weaver.schema.models import Timestamp

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceReference:
    occurred_at: datetime
    is_fallback: bool = False


class SummaryState:
    """Serialize review operations and persist their inputs and prepared outputs."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "summary.sqlite3"
        with self.lock(), self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS packages (id TEXT, revision INTEGER, package TEXT NOT NULL, script TEXT NOT NULL, PRIMARY KEY(id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS source_times (id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL)"
            )
            if "is_fallback" not in {
                row[1] for row in db.execute("PRAGMA table_info(source_times)")
            }:
                # Existing rows did not distinguish occurrence from capture.
                # Conservatively preserve historical values for these rows.
                db.execute(
                    "ALTER TABLE source_times ADD COLUMN is_fallback INTEGER NOT NULL DEFAULT 1"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviews (id TEXT, revision INTEGER, verdict TEXT NOT NULL, prepared TEXT NOT NULL, stage TEXT NOT NULL, PRIMARY KEY(id, revision))"
            )

    @contextmanager
    def lock(self) -> Iterator[None]:
        fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            with db:
                yield db
        finally:
            db.close()

    def save_package(
        self,
        package: ReviewPackage,
        script: str,
        contact_name: str | None = None,
        *,
        source_occurred_at: datetime | None = None,
        source_time_is_fallback: bool = False,
    ) -> None:
        """Persist an immutable revision, rejecting conflicting snapshots."""
        with self.connect() as db:
            if source_occurred_at is not None:
                timestamp = TypeAdapter(Timestamp).validate_python(source_occurred_at)
                row = db.execute(
                    "SELECT occurred_at, is_fallback FROM source_times WHERE id=?",
                    (package.package_id,),
                ).fetchone()
                if row and (
                    datetime.fromisoformat(row[0]) != timestamp
                    or bool(row[1]) != source_time_is_fallback
                ):
                    raise ValueError("conflicting source reference time")
                db.execute(
                    "INSERT OR IGNORE INTO source_times (id, occurred_at, is_fallback) VALUES (?, ?, ?)",
                    (
                        package.package_id,
                        timestamp.isoformat(),
                        source_time_is_fallback,
                    ),
                )
            if contact_name:
                db.execute(
                    "INSERT OR IGNORE INTO contacts VALUES (?, ?)",
                    (package.package_id, contact_name),
                )
            row = db.execute(
                "SELECT package, script FROM packages WHERE id=? AND revision=?",
                (package.package_id, package.revision),
            ).fetchone()
            if row is not None and (
                ReviewPackage.model_validate_json(row[0]) != package or row[1] != script
            ):
                raise ValueError("conflicting package revision")
            db.execute(
                "INSERT OR IGNORE INTO packages VALUES (?, ?, ?, ?)",
                (
                    package.package_id,
                    package.revision,
                    package.model_dump_json(),
                    script,
                ),
            )

    def load_package(self, package_id: str, revision: int) -> tuple[ReviewPackage, str]:
        """Load the exact draft and Script after a restart."""
        with self.connect() as db:
            row = db.execute(
                "SELECT package, script FROM packages WHERE id=? AND revision=?",
                (package_id, revision),
            ).fetchone()
        if row is None:
            raise ValueError("unknown package revision")
        return ReviewPackage.model_validate_json(row[0]), str(row[1])

    def contact_name(self, package_id: str, source_id: str) -> str | None:
        return read_contact_name(self.path.parent, package_id, source_id)


def read_contact_name(root: Path, package_id: str, source_id: str) -> str | None:
    """Read optional metadata; legacy packages can recover Spinner's archived hint."""
    with closing(connect_snapshot(root / "summary.sqlite3")) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='contacts'").fetchone():
            row = db.execute(
                "SELECT name FROM contacts WHERE id=?", (package_id,)
            ).fetchone()
            if row:
                return str(row[0])
    path = root / "spinner.sqlite3"
    if not path.is_file():
        return None
    with closing(connect_snapshot(path)) as db:
        rows = db.execute(
            "SELECT source FROM results WHERE source_id=? ORDER BY version",
            (source_id,),
        ).fetchall()
    for (raw,) in rows:
        source = json.loads(raw)
        counterpart = source.get("hints", {}).get("counterpart")
        if isinstance(counterpart, str) and counterpart.strip():
            return counterpart
    return None


def read_source_reference(root: Path, package: ReviewPackage) -> SourceReference:
    """Recover immutable source time, never the current processing clock.

    Source metadata in Spinner retains captured_at when occurrence is unknown.
    Old/imported packages without Source metadata use their earliest stage time.
    """
    with closing(connect_snapshot(root / "summary.sqlite3")) as db:
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='source_times'"
        ).fetchone():
            has_flag = "is_fallback" in {
                row[1] for row in db.execute("PRAGMA table_info(source_times)")
            }
            columns = "occurred_at, is_fallback" if has_flag else "occurred_at, 1"
            row = db.execute(
                f"SELECT {columns} FROM source_times WHERE id=?", (package.package_id,)
            ).fetchone()
            if row:
                return SourceReference(
                    TypeAdapter(Timestamp).validate_python(row[0]), bool(row[1])
                )
    path = root / "spinner.sqlite3"
    if path.is_file():
        with closing(connect_snapshot(path)) as db:
            rows = db.execute(
                "SELECT source FROM results WHERE source_id=? ORDER BY version",
                (package.source_id,),
            ).fetchall()
        for (raw,) in rows:
            source = json.loads(raw)
            timestamp = source.get("occurred_at") or source.get("captured_at")
            if timestamp is not None:
                return SourceReference(
                    TypeAdapter(Timestamp).validate_python(timestamp),
                    source.get("occurred_at") is None,
                )
    if not package.stage_times:
        raise ValueError("missing source reference time")
    logger.warning("using estimated source time from review stages")
    return SourceReference(min(package.stage_times.values()), True)


def read_source_occurred_at(root: Path, package: ReviewPackage) -> datetime:
    """Compatibility accessor for callers that only need the timestamp."""
    return read_source_reference(root, package).occurred_at
