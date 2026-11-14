"""Read Fabric by a durable sequence cursor and enqueue Actions."""

import logging

from weaver.fabric import Fabric, ThreadQuery

from .calendar import derive_calendar_actions
from .outbox import ActionOutbox

logger = logging.getLogger("weaver.action")


class ActionEngine:
    """Derive Actions from new Fabric rows; enqueue and cursor commit are atomic.

    Judging a schedule needs the whole history (a later decision unblocks an
    earlier schedule), so a run that sees new rows re-derives from the full
    snapshot. Idempotency keys make that safe: an Action already in the outbox
    is never created twice, whether the cursor moved or was reset.
    """

    def __init__(self, fabric: Fabric, outbox: ActionOutbox) -> None:
        self.fabric = fabric
        self.outbox = outbox

    def run(self) -> int:
        """Enqueue what is newly needed and return how many Actions were added."""
        cursor = self.outbox.cursor()
        everything = self.fabric.entries(ThreadQuery(include_executions=True))
        high = max((t.seq for t in everything), default=0)
        if high <= cursor:
            return 0
        executions = {t.thread_id: t for t in everything if t.kind == "실행"}
        business = [t for t in everything if t.kind != "실행"]
        needed = derive_calendar_actions(
            business,
            self.outbox.lineage,
            baseline=self.outbox.baseline() or 0,
            executions=executions,
        )
        added = self.outbox.commit(needed, high)
        if added:
            logger.info("actions enqueued: %d", added)
        return added
