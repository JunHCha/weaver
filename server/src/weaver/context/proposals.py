"""Location proposals: suggested tree nodes that wait for a person's decision.

A proposal is deduplicated by fingerprint. A rejected one is never proposed
again; accepting one (creating the node, moving Threads) is the caller's job.
"""

import hashlib
import json
import logging
import os
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from weaver.context import DictionarySnapshot
from weaver.schema.models import LocationProposal

logger = logging.getLogger(__name__)
Kind = Literal["location", "unclassified"]
Reason = Literal["unknown_path", "project_promotion", "reextract_unclassified"]
Status = Literal["pending", "accepted", "rejected"]


class ProposalDraft(BaseModel):
    """The arguments of ``LocationProposals.propose``, kept until Fabric commits."""

    kind: Kind
    reason: Reason
    sphere: str
    path: str | None = None
    parent: str | None = None
    fallback: str
    thread_ids: list[str]
    source_ids: list[str]
    key: str | None = None


def nearest_parent(dictionary: DictionarySnapshot, path: str) -> str | None:
    """The closest existing tree ancestor of ``path`` in the dictionary."""
    parts = [p for p in unicodedata.normalize("NFC", path).split("/") if p]
    for end in range(len(parts) - 1, 0, -1):
        entry = dictionary.resolve("/".join(parts[:end]))
        if entry is not None and entry.source == "tree":
            return entry.name
    return None


def _fingerprint(
    kind: str, path: str | None, sphere: str, reason: str, key: str | None
) -> str:
    parts = (
        [kind, reason, key or ""] if kind == "unclassified" else [kind, path or sphere]
    )
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class LocationProposals:
    def __init__(self, state_root: Path) -> None:
        self.path = state_root / "proposals.sqlite3"
        state_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS location_proposals ("
                "fingerprint TEXT PRIMARY KEY, "
                "proposal_id TEXT NOT NULL UNIQUE, "
                "status TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, "
                "payload TEXT NOT NULL)"
            )

    def _secure(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm", "-journal"):
            with suppress(FileNotFoundError):
                os.chmod(f"{self.path}{suffix}", 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._secure()
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            yield db
        self._secure()

    def propose(
        self,
        *,
        kind: Kind,
        reason: Reason,
        sphere: str,
        path: str | None,
        fallback: str,
        thread_ids: list[str],
        source_ids: list[str],
        now: datetime,
        parent: str | None = None,
        key: str | None = None,
    ) -> LocationProposal | None:
        """Record or merge a pending proposal; None when it was rejected before."""
        path = unicodedata.normalize("NFC", path) if path else None
        fingerprint = _fingerprint(kind, path, sphere, reason, key)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM location_proposals WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                created = LocationProposal(
                    schema_version=2,
                    proposal_id="prp_" + fingerprint[:32],
                    kind=kind,
                    reason=reason,
                    sphere=sphere,
                    path=path,
                    parent=parent,
                    fallback=fallback,
                    thread_ids=list(dict.fromkeys(thread_ids)),
                    source_ids=list(dict.fromkeys(source_ids)),
                    count=max(1, len(set(thread_ids))),
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
                self._save(db, fingerprint, created)
                db.execute("COMMIT")
                return created
            current = LocationProposal.model_validate_json(row[0])
            if current.status == "rejected":
                db.execute("ROLLBACK")
                return None
            if current.status == "accepted":
                db.execute("ROLLBACK")
                if set(thread_ids) - set(current.thread_ids):
                    logger.info("new evidence for an accepted location proposal")
                return current
            threads = list(dict.fromkeys([*current.thread_ids, *thread_ids]))
            sources = list(dict.fromkeys([*current.source_ids, *source_ids]))
            if threads == current.thread_ids and sources == current.source_ids:
                db.execute("ROLLBACK")
                return current
            merged = current.model_copy(
                update={
                    "thread_ids": threads,
                    "source_ids": sources,
                    "count": max(1, len(threads)),
                    "updated_at": max(now, current.updated_at),
                }
            )
            self._save(db, fingerprint, merged)
            db.execute("COMMIT")
            return merged

    @staticmethod
    def _save(db: sqlite3.Connection, fingerprint: str, item: LocationProposal) -> None:
        db.execute(
            "INSERT OR REPLACE INTO location_proposals VALUES (?, ?, ?, ?, ?)",
            (
                fingerprint,
                item.proposal_id,
                item.status,
                item.updated_at.isoformat(),
                item.model_dump_json(),
            ),
        )

    def _list(self, status: Status | None) -> list[LocationProposal]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM location_proposals "
                "WHERE (?1 IS NULL OR status=?1) "
                "ORDER BY updated_at DESC, proposal_id",
                (status,),
            ).fetchall()
        return [LocationProposal.model_validate_json(r[0]) for r in rows]

    def pending(self) -> list[LocationProposal]:
        return self._list("pending")

    def list(self, status: Status | None = None) -> list[LocationProposal]:
        return self._list(status)

    def get(self, proposal_id: str) -> LocationProposal | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM location_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        return LocationProposal.model_validate_json(row[0]) if row else None

    def decide(
        self,
        proposal_id: str,
        status: Literal["accepted", "rejected"],
        now: datetime,
    ) -> LocationProposal:
        """Mark a pending proposal; repeating the same decision is a no-op."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT fingerprint, payload FROM location_proposals "
                "WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(proposal_id)
            current = LocationProposal.model_validate_json(row[1])
            if current.status == status:
                db.execute("ROLLBACK")
                return current
            if current.status != "pending":
                db.execute("ROLLBACK")
                raise ValueError("proposal already decided")
            decided = current.model_copy(
                update={"status": status, "updated_at": max(now, current.updated_at)}
            )
            self._save(db, row[0], decided)
            db.execute("COMMIT")
            return decided
