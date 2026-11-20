"""Read-only archive policy adapter; Webex private provenance is added by W1-B1.

Shared dictionary prose lacks complete per-entry lineage. Check the superset
of approved/prepared review and Fabric origins; unprocessed raw captures are
not shared context. Wiki also supplies its private page/section origins.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from weaver.config import ServerConfig
from weaver.context import ContextDictionary
from weaver.readonly import connect_snapshot
from weaver.schema import Manifest, ReviewPackage, Source, Thread

from .policy import LlmPolicy


class ArchiveProvenanceResolver:
    def __init__(self, archive_root: Path, *, snapshot: bool = False) -> None:
        self.index = archive_root / "index.sqlite3"
        self.snapshot = snapshot

    def _connect(self) -> sqlite3.Connection:
        if self.snapshot:
            return connect_snapshot(self.index)
        # A live daemon must consume committed WAL contents. Opening mode=ro
        # never initializes the archive, sweeps payloads or publishes Receipts.
        return sqlite3.connect(f"{self.index.resolve().as_uri()}?mode=ro", uri=True)

    def resolve(self, source_id: str) -> tuple[str, str | None] | None:
        if not self.index.is_file():
            return None
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT source_json FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
            if row is None:
                return None
            source = Source.model_validate_json(row[0])
            captures = [
                Manifest.model_validate_json(raw)
                for (raw,) in db.execute(
                    "SELECT manifest_json FROM captures WHERE source_id=?", (source_id,)
                )
            ]
        # Content dedup must not erase a second, differently classified origin.
        if not captures or any(c.channel != source.channel for c in captures):
            return None
        if source.channel not in {"call", "voice_memo", "kakao", "share_text"}:
            return None  # W1-B1 resolves Webex's private Space ID here.
        return source.channel, None

    def source_ids(self) -> tuple[str, ...]:
        if not self.index.is_file():
            return ()
        with closing(self._connect()) as db:
            return tuple(
                str(sid) for (sid,) in db.execute("SELECT source_id FROM sources")
            )


def configured_policy(config: ServerConfig, *, snapshot: bool = False) -> LlmPolicy:
    resolver = ArchiveProvenanceResolver(config.archive_root, snapshot=snapshot)

    def shared_sources() -> tuple[str, ...]:
        ids: set[str] = set()
        fabric = config.state_root / "fabric.sqlite3"
        if fabric.is_file():
            db = (
                connect_snapshot(fabric)
                if snapshot
                else sqlite3.connect(f"{fabric.resolve().as_uri()}?mode=ro", uri=True)
            )
            with closing(db):
                ids.update(
                    Thread.model_validate_json(raw).source_id
                    for (raw,) in db.execute("SELECT payload FROM threads")
                )
        reviews = config.state_root / "summary.sqlite3"
        if reviews.is_file():
            db = (
                connect_snapshot(reviews)
                if snapshot
                else sqlite3.connect(f"{reviews.resolve().as_uri()}?mode=ro", uri=True)
            )
            with closing(db):
                ids.update(
                    ReviewPackage.model_validate_json(raw).source_id
                    for (raw,) in db.execute(
                        "SELECT p.package FROM packages p JOIN reviews r "
                        "ON p.id=r.id AND p.revision=r.revision "
                        "WHERE r.stage IN ('PREPARED','APPLIED') "
                        "AND json_extract(r.verdict, '$.action') IN ('approve','edit')"
                    )
                )
        dictionary = config.state_root / "context.sqlite3"
        if not ids and dictionary.is_file():
            if ContextDictionary.open_readonly(dictionary).snapshot().entries:
                ids.add("unknown_dictionary_provenance")
        return tuple(sorted(ids))

    return LlmPolicy(
        resolver,
        settings=config.privacy,
        # W1-B3 owns config.shepherds.webex.exclude_space_ids. The later PR
        # wires that tuple as frozenset here; None deliberately denies Webex.
        excluded_space_ids=None,
        shared_sources=shared_sources,
    )
