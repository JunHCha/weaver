"""Durable journal for summary drafts and review side effects."""

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from weaver.schema import ReviewPackage


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

    def save_package(self, package: ReviewPackage, script: str) -> None:
        """Persist an immutable revision, rejecting conflicting snapshots."""
        with self.connect() as db:
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
