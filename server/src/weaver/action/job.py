"""One maintenance pass of the Action path: results in, Actions derived, files out."""

from weaver.fabric import Fabric, ThreadQuery

from .courier import ActionCourier
from .engine import ActionEngine
from .outbox import ActionOutbox


class ActionJob:
    """Runs from the resident loop; every step is safe to repeat."""

    def __init__(
        self, fabric: Fabric, outbox: ActionOutbox, courier: ActionCourier
    ) -> None:
        self.fabric = fabric
        self.outbox = outbox
        self.courier = courier
        self.engine = ActionEngine(fabric, outbox)

    def run(self) -> None:
        if self.outbox.baseline() is None:
            # An outbox created beside legacy schedule files starts after
            # everything already approved: the old phone flow may have
            # registered those events, and re-registering would duplicate them.
            head = max(
                (
                    t.seq
                    for t in self.fabric.entries(ThreadQuery(include_executions=True))
                ),
                default=0,
            )
            self.outbox.initialize(head if self.courier.legacy_present() else 0)
        self.courier.retire_legacy()
        self.courier.ingest()
        self.engine.run()
        self.courier.publish()
