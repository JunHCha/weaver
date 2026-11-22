"""Per-feature transport switch and the resident worker that drives it.

``[mobile] transport`` (or ``[mobile.features]``) chooses ``files`` or ``http``
for capture, review and action. The last applied choice is recorded, so a
change back to ``files`` is noticed on the next pass and the unfinished items
are published to the Syncthing folders again. Items already delivered or
applied over HTTP are blocked from running twice by the shared journals.
"""

import logging
from datetime import UTC, datetime

from weaver.action.courier import ActionCourier
from weaver.alert.delivery import AlertDelivery
from weaver.archive import ArchiveStore
from weaver.config import TRANSPORT_FEATURES, MobileSettings, ServerConfig

from .applier import MobileApplier
from .reviews import ReviewService
from .store import MobileStore

logger = logging.getLogger("weaver.mobile")


def reconcile_transports(
    settings: MobileSettings,
    store: MobileStore,
    reviews: ReviewService,
    actions: ActionCourier,
    config: ServerConfig,
) -> list[str]:
    """Record the configured transports; republish when a feature returns to files."""
    alerts = AlertDelivery(config)
    republished: list[str] = []
    for feature in TRANSPORT_FEATURES:
        wanted = settings.transport_for(feature)
        before = store.recorded_transport(feature)
        if before == wanted:
            continue
        if before == "http" and wanted == "files":
            if feature == "capture":
                ArchiveStore.from_config(config).republish_receipts()
            elif feature == "review":
                reviews.republish(config.courier.proposals)
            elif feature == "action":
                actions.publish()
            elif feature == "alert":
                alerts.republish()
            republished.append(feature)
            logger.info("mobile transport %s: http -> files, republished", feature)
        store.record_transport(feature, wanted)
    return republished


class MobileRuntime:
    """Maintenance task of the resident daemon."""

    def __init__(
        self,
        config: ServerConfig,
        store: MobileStore,
        applier: MobileApplier,
        reviews: ReviewService,
        actions: ActionCourier,
    ) -> None:
        self.config = config
        self.store = store
        self.applier = applier
        self.reviews = reviews
        self.actions = actions
        self._recovered = False

    def run(self) -> None:
        if not self._recovered:
            self.applier.recover()
            self._recovered = True
        reconcile_transports(
            self.config.mobile, self.store, self.reviews, self.actions, self.config
        )
        self.applier.run()
        if self.config.mobile.transport_for("alert") == "http":
            AlertDelivery(self.config).history.collect(
                self.config.mobile.alert_body_days,
                [
                    device.device_id
                    for device in self.store.devices()
                    if device.active and "review" in device.scopes
                ],
                datetime.now(UTC),
            )
