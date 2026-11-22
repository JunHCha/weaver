"""Producer of the phone change feed (persistent checkpoint + reconciliation).

Instead of an in-memory queue, every call compares the current domain state
with the last recorded entry of each resource and records only the difference.
A crash cannot lose a change: the next reconciliation finds it again.
"""

from collections.abc import Callable
from dataclasses import dataclass

from weaver.action.courier import ActionCourier
from weaver.alert.history import AlertHistory

from .pipeline import PipelineReadModel, PipelineUnavailable
from .reviews import ReviewService, etag
from .store import FeedItem, MobileStore, canonical, content_hash

# What every phone before the feed grew new kinds understands. A client that does
# not say which kinds it handles (``kinds`` query) only ever gets these.
LEGACY_KINDS = frozenset({"review", "action", "fabric"})
# kind -> scope that may see it. Alerts do not depend on review's transport.
KIND_SCOPES = {
    "review": "review",
    "action": "action",
    "alert": "review",
    "pipeline": "read",
    "wiki": "read",
    "fabric": "read",
}


@dataclass(frozen=True)
class FeedSources:
    """Optional producers; a source that is off or unreadable keeps its entries."""

    review_http: Callable[[], bool] = lambda: True
    alerts: AlertHistory | None = None
    alerts_http: Callable[[], bool] = lambda: False
    pipeline: PipelineReadModel | None = None
    pipeline_http: Callable[[], bool] = lambda: False
    wiki_hints: Callable[[], dict[str, str] | None] = lambda: None


class FeedProducer:
    def __init__(
        self,
        store: MobileStore,
        reviews: ReviewService,
        actions: ActionCourier | None,
        fabric_head: Callable[[], int],
        action_dispatch: Callable[[], bool] = lambda: True,
        sources: FeedSources | None = None,
    ) -> None:
        self.store = store
        self.reviews = reviews
        self.actions = actions
        self.fabric_head = fabric_head
        self.action_dispatch = action_dispatch
        self.sources = sources or FeedSources()

    def served_kinds(self) -> frozenset[str]:
        """Kinds this server currently offers.

        A feature that returned to files drops its kind from here. Its recorded
        entries stay untouched and hidden: leaving a route is not deleting the
        resource, so no tombstone is ever written for it. When the kind is served
        again, every current entry is announced anew (see ``reconcile``).
        """
        kinds = {"fabric", "wiki"}
        if self.sources.review_http():
            kinds.add("review")
        if self.actions is not None and self.action_dispatch():
            kinds.add("action")
        if self.sources.alerts is not None and self.sources.alerts_http():
            kinds.add("alert")
        if self.sources.pipeline is not None and self.sources.pipeline_http():
            kinds.add("pipeline")
        return frozenset(kinds)

    def reconcile(self) -> int:
        served = self.served_kinds()
        current: dict[tuple[str, str], FeedItem] = {}
        # Kinds whose resources were actually enumerated this pass; only these may
        # tombstone what disappeared. Off or unreadable sources are left alone.
        complete: set[str] = {"fabric"}
        # Resources that exist but could not be read this pass: never tombstoned.
        unreadable: set[tuple[str, str]] = set()

        def want(
            kind: str, resource: str, revision: int | None, body: str | None
        ) -> None:
            digest = content_hash(canonical([revision, body]))
            current[(kind, resource)] = FeedItem(
                0, kind, resource, revision, "upsert", digest, body
            )

        if "review" in served:
            complete.add("review")
            for package in self.reviews.open_packages():
                want(
                    "review",
                    package.package_id,
                    package.revision,
                    canonical({"etag": etag(package), "stage": package.stage}),
                )
        if "action" in served and self.actions is not None:
            complete.add("action")
            for action in self.actions.dispatchable():
                want("action", action.action_id, action.input_version, None)
        want("fabric", "threads", self.fabric_head(), None)
        sources = self.sources
        if "alert" in served and sources.alerts is not None:
            # Same predicate as /alerts: delivery rows whose body is still held
            # (AlertHistory.bodies / .body, served by api.list_alerts/get_alert).
            retained, _expired = sources.alerts.delivery_ids()
            complete.add("alert")
            for alert_id in retained:
                want("alert", alert_id, None, None)
            # An expired body is no longer served; its hint is withdrawn.
        if "pipeline" in served and sources.pipeline is not None:
            try:
                snapshot = (
                    sources.pipeline.snapshot()
                    if sources.pipeline.available()
                    else None
                )
            except PipelineUnavailable:
                snapshot = None  # keep the recorded entries until it is readable
            if snapshot is not None:
                complete.add("pipeline")
                unreadable |= {("pipeline", sid) for sid in snapshot.skipped}
                for entry in snapshot.entries:
                    want(
                        "pipeline",
                        entry.source_id,
                        entry.revision,
                        canonical({"etag": '"' + entry.etag + '"'}),
                    )
        hints = sources.wiki_hints()
        if hints is not None:
            complete.add("wiki")
            for page_id, sha in hints.items():
                want("wiki", page_id, None, canonical({"etag": f'"{sha}"'}))
        # A kind that was hidden and is served again: a phone may have taken its
        # snapshot while the feature ran over files, so announce everything anew.
        hidden = self.store.feed_hidden()
        returning = (hidden & served) & complete
        recorded = self.store.feed_current()
        changes: list[FeedItem] = []
        for key, item in current.items():
            before = recorded.get(key)
            if (
                before is None
                or before.op == "tombstone"
                or before.digest != item.digest
                or key[0] in returning
            ):
                changes.append(item)
        for key, before in recorded.items():
            if (
                key in current
                or key in unreadable
                or before.op != "upsert"
                or key[0] not in complete
            ):
                continue
            changes.append(
                FeedItem(
                    0, key[0], key[1], before.revision, "tombstone", "tombstone", None
                )
            )
        self.store.feed_record(changes)
        now_hidden = (frozenset(KIND_SCOPES) - served) | ((hidden & served) - complete)
        if now_hidden != hidden:
            self.store.set_feed_hidden(now_hidden)
        return len(changes)
