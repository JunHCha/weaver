"""Producer of the phone change feed (persistent checkpoint + reconciliation).

Instead of an in-memory queue, every call compares the current domain state
with the last recorded entry of each resource and records only the difference.
A crash cannot lose a change: the next reconciliation finds it again.
"""

from collections.abc import Callable
from dataclasses import dataclass

from weaver.action.courier import ActionCourier
from weaver.alert.history import AlertHistory

from .pipeline import PipelineReadModel
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
        resource, so no tombstone is ever written for it.
        """
        kinds = {"review", "fabric", "wiki"}
        if self.actions is not None and self.action_dispatch():
            kinds.add("action")
        if self.sources.alerts is not None and self.sources.alerts_http():
            kinds.add("alert")
        if self.sources.pipeline is not None and self.sources.pipeline_http():
            kinds.add("pipeline")
        return frozenset(kinds)

    def reconcile(self) -> int:
        current: dict[tuple[str, str], FeedItem] = {}
        # Kinds whose resources were actually enumerated this pass; only these may
        # tombstone what disappeared. Off or unreadable sources are left alone.
        complete: set[str] = {"review", "fabric"}

        def want(
            kind: str, resource: str, revision: int | None, body: str | None
        ) -> None:
            digest = content_hash(canonical([revision, body]))
            current[(kind, resource)] = FeedItem(
                0, kind, resource, revision, "upsert", digest, body
            )

        for package in self.reviews.open_packages():
            want(
                "review",
                package.package_id,
                package.revision,
                canonical({"etag": etag(package), "stage": package.stage}),
            )
        if self.actions is not None and self.action_dispatch():
            complete.add("action")
            for action in self.actions.dispatchable():
                want("action", action.action_id, action.input_version, None)
        head = self.fabric_head()
        want("fabric", "threads", head, None)
        sources = self.sources
        expired_alerts: set[tuple[str, str]] = set()
        if sources.alerts is not None and sources.alerts_http():
            retained, expired = sources.alerts.delivery_ids()
            complete.add("alert")
            for alert_id in retained:
                want("alert", alert_id, None, None)
            # An expired body is no longer served; the delivery ID stays known.
            expired_alerts = {("alert", alert_id) for alert_id in expired}
        if (
            sources.pipeline is not None
            and sources.pipeline_http()
            and sources.pipeline.available()
        ):
            complete.add("pipeline")
            for entry in sources.pipeline.entries():
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
        recorded = self.store.feed_current()
        changes: list[FeedItem] = []
        for key, item in current.items():
            before = recorded.get(key)
            if (
                before is None
                or before.op == "tombstone"
                or before.digest != item.digest
            ):
                changes.append(item)
        for key, before in recorded.items():
            if key in current or before.op != "upsert" or key[0] not in complete:
                continue
            if key[0] == "alert" and key not in expired_alerts:
                continue
            changes.append(
                FeedItem(
                    0, key[0], key[1], before.revision, "tombstone", "tombstone", None
                )
            )
        self.store.feed_record(changes)
        return len(changes)
