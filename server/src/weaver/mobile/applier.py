"""Finish journaled phone operations inside the resident runtime.

HTTP only commits the durable inbox row and answers 202. This worker applies
each row through the same domain journals the file transport uses
(``VerdictIngest`` decision application, ``ActionCourier`` results), so a request
delivered twice, or over both transports, changes the domain once.
``applied`` is recorded only after the domain confirms it.
"""

import logging

from weaver.action.courier import ActionCourier
from weaver.fabric import FabricError
from weaver.ingest import CourierFileError, VerdictIngest
from weaver.schema import ActionResult, Verdict

from .store import MobileStore, Operation

logger = logging.getLogger("weaver.mobile")
# Courier codes that normally clear on their own; the operation stays applying.
_RETRYABLE = {
    "apply_waiting",
    "apply_backoff",
    "apply_failed",
    "apply_unconfirmed",
    "store_failed",
}
_STALE = {"stale_rev"}


class MobileApplier:
    def __init__(
        self, store: MobileStore, ingest: VerdictIngest, actions: ActionCourier
    ) -> None:
        self.store = store
        self.ingest = ingest
        self.actions = actions

    def recover(self) -> int:
        """A restart retries stopped verdicts, like the file path does."""
        count = 0
        self.ingest.forget_external_failures()
        for op in self.store.stopped():
            self.store.update_operation(op.operation_id, "queued")
            count += 1
        return count

    def run(self) -> None:
        for op in self.store.pending("verdict"):
            self._verdict(op)
        for op in self.store.pending("action_result"):
            self._action_result(op)

    def _verdict(self, op: Operation) -> None:
        if op.payload is None:
            self.store.update_operation(op.operation_id, "failed", error="payload_lost")
            return
        try:
            self.ingest.apply_external(
                op.operation_id, Verdict.model_validate_json(op.payload)
            )
        except CourierFileError as error:
            code = error.code
            if code in _RETRYABLE:
                self.store.update_operation(op.operation_id, "applying", error=code)
            elif code in _STALE:
                self.store.update_operation(
                    op.operation_id, "stale", error=code, drop_payload=True
                )
            else:
                self.store.update_operation(op.operation_id, "failed", error=code)
            logger.info("mobile verdict %s: %s", op.operation_id, code)
            return
        self.store.update_operation(op.operation_id, "applied", drop_payload=True)

    def _action_result(self, op: Operation) -> None:
        if op.payload is None:
            self.store.update_operation(op.operation_id, "failed", error="payload_lost")
            return
        try:
            self.actions.apply_result(ActionResult.model_validate_json(op.payload))
        except CourierFileError as error:
            if error.code == "apply_backoff":
                self.store.update_operation(
                    op.operation_id, "applying", error=error.code
                )
            else:
                self.store.update_operation(op.operation_id, "failed", error=error.code)
            logger.info("mobile action result %s: %s", op.operation_id, error.code)
            return
        except FabricError as error:
            self.store.update_operation(
                op.operation_id, "applying", error="fabric_failed"
            )
            logger.warning(
                "mobile action result %s: %s", op.operation_id, type(error).__name__
            )
            return
        self.store.update_operation(op.operation_id, "applied", drop_payload=True)
