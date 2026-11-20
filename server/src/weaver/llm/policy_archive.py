"""Read-only archive policy adapter; Webex Space IDs come from its private store.

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
from weaver.schema import Manifest, ReviewPackage, Source, ThreadDraft
from weaver.spinner.webex import WebexProvenanceStore

from .policy import (
    ALLOWED_CHANNELS,
    UNKNOWN_DICTIONARY_SOURCE,
    UNKNOWN_LINEAGE,
    UNKNOWN_WIKI_SOURCE,
    LlmPolicy,
)


class ArchiveProvenanceResolver:
    def __init__(
        self,
        archive_root: Path,
        *,
        snapshot: bool = False,
        state_root: Path | None = None,
    ) -> None:
        self.index = archive_root / "index.sqlite3"
        self.snapshot = snapshot
        self.state_root = state_root

    def _webex_space(self, source: Source) -> str | None:
        """Space ID from Webex's private store, which is never created here."""
        if self.state_root is None:
            return None
        path = self.state_root / "webex-provenance.sqlite3"
        if not path.is_file():
            return None
        db = (
            connect_snapshot(path)
            if self.snapshot
            else sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        )
        with closing(db):
            row = db.execute(
                "SELECT body FROM provenance WHERE source_id=?", (source.source_id,)
            ).fetchone()
        if row is None:
            return None
        value = WebexProvenanceStore._decode(row[0])
        if value.sha256 != source.sha256:
            return None  # lineage of different content: unknown
        return value.space_id

    def has_webex_source(self) -> bool:
        if not self.index.is_file():
            return False
        with closing(self._connect()) as db:
            return (
                db.execute(
                    "SELECT 1 FROM sources "
                    "WHERE json_extract(source_json, '$.channel')='webex' LIMIT 1"
                ).fetchone()
                is not None
            )

    def _connect(self) -> sqlite3.Connection:
        if self.snapshot:
            return connect_snapshot(self.index)
        # A live daemon must consume committed WAL contents. Opening mode=ro
        # never initializes the archive, sweeps payloads or publishes Receipts.
        return sqlite3.connect(f"{self.index.resolve().as_uri()}?mode=ro", uri=True)

    def normalize_source(self, source_id: str) -> str:
        """Human-only Fabric IDs without an archived Source have unknown lineage.

        Wiki observations and phone decisions predate any origin marker. Inspect
        all references rather than guessing from IDs or trusting one human row.
        An archived Source always retains its original policy classification.
        """
        if source_id in UNKNOWN_LINEAGE:
            return source_id
        if self.index.is_file():
            with closing(self._connect()) as db:
                if db.execute(
                    "SELECT 1 FROM sources WHERE source_id=?", (source_id,)
                ).fetchone():
                    return source_id
        if self.state_root is None:
            return source_id
        path = self.state_root / "fabric.sqlite3"
        if not path.is_file():
            return source_id
        db = (
            connect_snapshot(path)
            if self.snapshot
            else sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        )
        with closing(db):
            references = [
                ThreadDraft.model_validate_json(raw)
                for (raw,) in db.execute(
                    "SELECT payload FROM threads "
                    "WHERE json_extract(payload, '$.source_id')=?",
                    (source_id,),
                )
            ]
        if references and all(t.actor == "human" for t in references):
            return UNKNOWN_WIKI_SOURCE
        return source_id

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
        if source.channel == "webex":
            space_id = self._webex_space(source)
            return ("webex", space_id) if space_id else None
        if source.channel not in ALLOWED_CHANNELS:
            return None
        return source.channel, None

    def source_ids(self) -> tuple[str, ...]:
        if not self.index.is_file():
            return ()
        with closing(self._connect()) as db:
            return tuple(
                str(sid) for (sid,) in db.execute("SELECT source_id FROM sources")
            )


def configured_policy(config: ServerConfig, *, snapshot: bool = False) -> LlmPolicy:
    resolver = ArchiveProvenanceResolver(
        config.archive_root, snapshot=snapshot, state_root=config.state_root
    )

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
                # Fabric stores drafts; the append sequence lives in its own column.
                ids.update(
                    ThreadDraft.model_validate_json(raw).source_id
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
                ids.add(UNKNOWN_DICTIONARY_SOURCE)
        return tuple(sorted(ids))

    return LlmPolicy(
        resolver,
        settings=config.privacy,
        # An empty tuple means no Space is excluded; a missing Space ID or
        # unresolvable Webex Source is still denied by the resolver.
        excluded_space_ids=frozenset(config.shepherds.webex.exclude_space_ids),
        shared_sources=shared_sources,
        webex_present=resolver.has_webex_source,
        normalize_source=resolver.normalize_source,
    )
