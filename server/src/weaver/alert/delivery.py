"""Private Alert bodies and the rollback file projection (ADR-049 / I3).

Receipt ack never means dismiss. The phone owns the (server_id, alert_id)
ledger, including notification publication and local read/dismiss state.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from weaver.config import ServerConfig
from weaver.courier.atomic import fsync_directory, write_json_atomic
from weaver.schema import Alert

from .history import AlertHistory

logger = logging.getLogger("weaver.alert")


@dataclass(frozen=True)
class ReplayResult:
    published: int
    expired: int


class AlertDelivery:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.history = AlertHistory(config.state_root / "alerts.sqlite3")

    def reconcile(self) -> int:
        return self.import_files(
            retire=self.config.mobile.transport_for("alert") == "http"
        )

    def import_files(self, *, retire: bool = False) -> int:
        """Import actual extant files only; history cannot reconstruct old bodies.

        Invalid/conflicting files remain available for diagnosis; other files proceed.
        """
        count = 0
        for path in self.config.courier.proposals.glob("alert_*.json"):
            try:
                alert = Alert.model_validate_json(path.read_bytes())
                if path.name != f"alert_{alert.alert_id}.json":
                    raise ValueError("alert_filename_mismatch")
                self.history.stage(
                    alert, self.history.basis(alert.alert_id) or "", projected=True
                )
                if retire:
                    path.unlink()
                    fsync_directory(path.parent, strict=True)
            except (ValueError, OSError) as error:
                logger.warning(
                    "alert migration skipped: %s %s", path.name, type(error).__name__
                )
                continue
            count += 1
        return count

    def _publish(self, alert: Alert) -> Path:
        directory = self.config.courier.proposals
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"alert_{alert.alert_id}.json"
        if path.exists():
            if Alert.model_validate_json(path.read_bytes()) != alert:
                raise ValueError("alert_body_conflict")
        else:
            write_json_atomic(path, alert)
        fsync_directory(directory, strict=True)
        return path

    def publish_alert(self, alert: Alert) -> Path | None:
        """The detector must stage the body and history before delivery."""
        if self.history.body(alert.alert_id) != alert:
            raise ValueError("alert_not_staged")
        if self.config.mobile.transport_for("alert") == "files":
            return self._publish(alert)
        return None

    def republish(self) -> ReplayResult:
        """HTTP -> files includes acknowledged alerts, with identical IDs/bodies.

        The phone ledger suppresses notifications for already published/dismissed
        IDs and preserves unread bodies. Server acks cannot filter this replay.
        """
        alerts = self.history.bodies()
        for alert in alerts:
            self._publish(alert)
            self.history.projected(alert.alert_id)
        expired = self.history.expired_count()
        if expired:
            logger.warning("alert rollback missing expired bodies: %d", expired)
        return ReplayResult(len(alerts), expired)
