"""Runtime adapters: approved relations from the Loom journal and the tick-level
change feed that turns new Fabric facts into ``Publisher.publish(changed)``.
"""

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime

from weaver.fabric import EntryQuery, Fabric
from weaver.loom.review_store import ReviewStore
from weaver.schema import Proposal

from .notes import RelationLink
from .publisher import EntityDirectory, Publisher, PublishState

logger = logging.getLogger(__name__)

# A full pass catches what the recorded_at cursor cannot see: crash-recovered
# approvals appended later with an older recorded_at, renames, relation-only
# approvals. Unchanged files are never rewritten, so a full pass is cheap.
FULL_SYNC_INTERVAL_SECONDS = 6 * 3600.0
_CURSOR = "fabric_cursor"


class ReviewRelations:
    """Relations of APPLIED approve/edit reviews, endpoints resolved to IDs.

    ``this`` (the proposal's source) becomes the review's record notes, or the
    src_ ID when the approval produced no Entry.
    """

    def __init__(self, reviews: ReviewStore) -> None:
        self.reviews = reviews

    def relations(self) -> list[RelationLink]:
        with self.reviews.connect() as db:
            rows = db.execute(
                "SELECT verdict, result, approved, entity_ids FROM reviews "
                "WHERE stage='APPLIED' AND approved IS NOT NULL ORDER BY rowid"
            ).fetchall()
        links: list[RelationLink] = []
        for row in rows:
            try:
                links.extend(_row_links(*row))
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                # One unreadable journal row must not hide every other relation.
                logger.warning("publish relation row skipped: %s", type(error).__name__)
        return links


def _row_links(
    verdict: str, result: str, approved: str, entity_ids: str
) -> list[RelationLink]:
    if json.loads(verdict).get("verdict") not in ("approve", "edit"):
        return []
    proposal = Proposal.model_validate_json(approved)
    ids = {str(k): str(v) for k, v in json.loads(entity_ids).items()}
    records = tuple(str(item["entry_id"]) for item in json.loads(result))
    source = proposal.source.source_id
    this = records or (source,)
    return [
        RelationLink(
            rel.type,
            _resolve(rel.from_ref, ids, this),
            _resolve(rel.to, ids, this),
            rel.reason,
            source,
        )
        for rel in proposal.relations
    ]


def _resolve(ref: str, ids: dict[str, str], this: tuple[str, ...]) -> tuple[str, ...]:
    return this if ref == "this" else (ids.get(ref, ref),)


class VaultSync:
    """Runtime ``VaultPublisher``: publish entities touched since the last call.

    The cursor (last recorded_at plus the IDs seen at it) advances only after a
    successful publish, so a failed tick is retried by the next one. Failures
    are contained here and logged by type only, so the runtime tick (and its
    maintenance tasks) is never degraded by vault output problems.
    """

    def __init__(
        self,
        publisher: Publisher,
        fabric: Fabric,
        entities: EntityDirectory,
        state: PublishState,
        *,
        full_interval: float = FULL_SYNC_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.publisher = publisher
        self.fabric = fabric
        self.entities = entities
        self.state = state
        self.full_interval = full_interval
        self.clock = clock
        self._last_full: float | None = None
        self.failures = 0

    def publish(self) -> None:
        try:
            self._publish()
        except Exception as error:
            self.failures += 1
            logger.error("publish failed: %s", type(error).__name__)

    def _publish(self) -> None:
        now = self.clock()
        full = self._last_full is None or now - self._last_full >= self.full_interval
        raw = self.state.get(_CURSOR)
        since, seen = (None, set[str]()) if raw is None else _decode(raw)
        if full:
            entries = self.fabric.entries(EntryQuery(order_by="recorded_at"))
            changed = {e.entity_id for e in self.entities.index().entities}
        else:
            query = EntryQuery(since=since, order_by="recorded_at")
            entries = [e for e in self.fabric.entries(query) if e.entry_id not in seen]
            changed = set()
        changed |= {entity for item in entries for entity in item.entities}
        written = self.publisher.publish(sorted(changed))
        if written or self.publisher.conflicts:
            logger.info(
                "published %d notes, %d skipped",
                len(written),
                len(self.publisher.conflicts),
            )
        if full:
            self._last_full = now
        if entries:
            latest = max(item.recorded_at for item in entries)
            if since is None or latest > since:
                since, seen = latest, set()
            if latest == since:
                seen |= {e.entry_id for e in entries if e.recorded_at == latest}
            self.state.put(_CURSOR, _encode(since, seen))


def _encode(since: datetime, seen: set[str]) -> str:
    return json.dumps({"since": since.isoformat(), "seen": sorted(seen)})


def _decode(raw: str) -> tuple[datetime, set[str]]:
    data = json.loads(raw)
    return datetime.fromisoformat(data["since"]), set(data["seen"])
