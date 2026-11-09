"""Private durable review journal, separate from Fabric and synced files."""

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from weaver.config import ServerConfig
from weaver.schema import Proposal


class ReviewStore:
    """Serialize reviewers across processes; external effects use durable payloads.

    Each short SQLite transaction is FULL/fullfsync. The advisory lock spans
    external effects without holding a SQLite transaction open.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock():
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            with self.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3):
                    raise ValueError("unsupported Loom journal version")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS proposals (
                        proposal_id TEXT NOT NULL, rev INTEGER NOT NULL,
                        snapshot TEXT NOT NULL, PRIMARY KEY(proposal_id, rev));
                    CREATE TABLE IF NOT EXISTS reviews (
                        proposal_id TEXT NOT NULL, rev INTEGER NOT NULL,
                        verdict TEXT NOT NULL, result TEXT NOT NULL,
                        stage TEXT NOT NULL CHECK(stage IN ('PREPARED','APPLIED')),
                        PRIMARY KEY(proposal_id, rev));
                    CREATE TABLE IF NOT EXISTS revise_failures (
                        verdict TEXT PRIMARY KEY NOT NULL,
                        failures INTEGER NOT NULL);

                """)
                if version < 2:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("ALTER TABLE proposals ADD COLUMN source_id TEXT")
                    db.execute(
                        "UPDATE proposals SET source_id=json_extract(snapshot, '$.source.source_id')"
                    )
                    db.execute("ALTER TABLE reviews ADD COLUMN approved TEXT")
                    db.execute(
                        "ALTER TABLE reviews ADD COLUMN entity_ids TEXT NOT NULL DEFAULT '{}'"
                    )
                    db.execute(
                        "CREATE INDEX IF NOT EXISTS proposals_source ON proposals(source_id, rev)"
                    )
                    db.execute("PRAGMA user_version=2")
                if version < 3:
                    # #27: prompt/model versions of the call that produced a rev.
                    if not db.in_transaction:
                        db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "ALTER TABLE proposals ADD COLUMN versions TEXT NOT NULL DEFAULT '{}'"
                    )
                    db.execute("PRAGMA user_version=3")

    @classmethod
    def from_config(cls, config: ServerConfig) -> "ReviewStore":
        return cls(config.state_root / "loom.sqlite3")

    @contextmanager
    def lock(self) -> Iterator[None]:
        fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.path.with_suffix(".lock"), 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("PRAGMA checkpoint_fullfsync=ON")
            with db:
                yield db
        finally:
            db.close()

    def latest_rev(self, proposal_id: str) -> int | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT MAX(rev) FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return row[0] if row else None

    def save_proposal(
        self, proposal: Proposal, versions: Mapping[str, str] | None = None
    ) -> None:
        with self.connect() as db:
            self.insert_proposal(db, proposal, versions)

    @staticmethod
    def insert_proposal(
        db: sqlite3.Connection,
        proposal: Proposal,
        versions: Mapping[str, str] | None = None,
    ) -> None:
        """The first insert fixes versions; identical retries never rewrite them."""
        payload = proposal.model_dump_json()
        row = db.execute(
            "SELECT snapshot FROM proposals WHERE proposal_id=? AND rev=?",
            (proposal.proposal_id, proposal.rev),
        ).fetchone()
        if row and row[0] != payload:
            raise ValueError("conflicting proposal snapshot")
        db.execute(
            "INSERT OR IGNORE INTO proposals (proposal_id,rev,snapshot,source_id,versions) "
            "VALUES (?,?,?,?,?)",
            (
                proposal.proposal_id,
                proposal.rev,
                payload,
                proposal.source.source_id,
                json.dumps(dict(versions or {}), sort_keys=True),
            ),
        )

    def versions(self, proposal_id: str, rev: int) -> dict[str, str]:
        with self.connect() as db:
            row = db.execute(
                "SELECT versions FROM proposals WHERE proposal_id=? AND rev=?",
                (proposal_id, rev),
            ).fetchone()
        return dict(json.loads(row[0])) if row else {}

    def source_proposal(self, source_id: str) -> Proposal | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT snapshot FROM proposals WHERE source_id=? AND rev=1 ORDER BY rowid LIMIT 1",
                (source_id,),
            ).fetchone()
        return Proposal.model_validate_json(row[0]) if row else None

    def approval(self, proposal_id: str, rev: int) -> tuple[Proposal, dict[str, str]]:
        with self.connect() as db:
            row = db.execute(
                "SELECT approved,entity_ids FROM reviews WHERE proposal_id=? AND rev=?",
                (proposal_id, rev),
            ).fetchone()
        if row is None:
            raise ValueError("unknown review")
        return (
            Proposal.model_validate_json(row[0])
            if row[0]
            else self.proposal(proposal_id, rev),
            json.loads(row[1]),
        )

    def proposal(self, proposal_id: str, rev: int) -> Proposal:
        with self.connect() as db:
            row = db.execute(
                "SELECT snapshot FROM proposals WHERE proposal_id=? AND rev=?",
                (proposal_id, rev),
            ).fetchone()
        if row is None:
            raise ValueError("unknown proposal revision")
        return Proposal.model_validate_json(row[0])

    def review(self, proposal_id: str, rev: int) -> tuple[str, str, str] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT verdict,result,stage FROM reviews WHERE proposal_id=? AND rev=?",
                (proposal_id, rev),
            ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def is_applied(self, proposal_id: str, rev: int) -> bool:
        row = self.review(proposal_id, rev)
        return row is not None and row[2] == "APPLIED"

    def revise_failures(self, verdict: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT failures FROM revise_failures WHERE verdict=?", (verdict,)
            ).fetchone()
        return int(row[0]) if row else 0

    def record_revise_failure(self, verdict: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO revise_failures VALUES (?,1) "
                "ON CONFLICT(verdict) DO UPDATE SET failures=failures+1",
                (verdict,),
            )

    def pending(self, proposal_id: str) -> list[tuple[str, int]]:
        with self.connect() as db:
            return [
                (row[0], row[1])
                for row in db.execute(
                    "SELECT verdict,rev FROM reviews WHERE proposal_id=? "
                    "AND stage='PREPARED' ORDER BY rev",
                    (proposal_id,),
                )
            ]
