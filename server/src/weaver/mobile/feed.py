"""Producer of the phone change feed (persistent checkpoint + reconciliation).

Instead of an in-memory queue, every call compares the current domain state
with the last recorded entry of each resource and records only the difference.
A crash cannot lose a change: the next reconciliation finds it again.
"""

from collections.abc import Callable

from weaver.action.courier import ActionCourier

from .reviews import ReviewService, etag
from .store import FeedItem, MobileStore, canonical, content_hash


class FeedProducer:
    def __init__(
        self,
        store: MobileStore,
        reviews: ReviewService,
        actions: ActionCourier | None,
        fabric_head: Callable[[], int],
        action_dispatch: Callable[[], bool] = lambda: True,
    ) -> None:
        self.store = store
        self.reviews = reviews
        self.actions = actions
        self.fabric_head = fabric_head
        self.action_dispatch = action_dispatch

    def reconcile(self) -> int:
        current: dict[tuple[str, str], FeedItem] = {}

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
            for action in self.actions.dispatchable():
                want("action", action.action_id, action.input_version, None)
        head = self.fabric_head()
        want("fabric", "threads", head, None)
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
            if key not in current and before.op == "upsert":
                changes.append(
                    FeedItem(
                        0,
                        key[0],
                        key[1],
                        before.revision,
                        "tombstone",
                        "tombstone",
                        None,
                    )
                )
        self.store.feed_record(changes)
        return len(changes)
