"""Private Alert bodies and the rollback file projection (ADR-049 / I3).

Receipt ack never means dismiss. The phone owns the (server_id, alert_id)
ledger, including notification publication and local read/dismiss state.
"""

from pathlib import Path

from weaver.config import ServerConfig
from weaver.courier.atomic import fsync_directory, write_json_atomic
from weaver.schema import Alert

from .history import AlertHistory


class AlertDelivery:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.history = AlertHistory(config.state_root / "alerts.sqlite3")
        self.import_files()
        if config.mobile.transport_for("alert") == "http":
            self.retire_files()

    def import_files(self) -> int:
        """Import actual extant files only; history cannot reconstruct old bodies.

        Validate before touching files. Malformed/conflicting files stop migration
        and remain available for diagnosis rather than silently losing an alert.
        """
        count = 0
        for path in self.config.courier.proposals.glob("alert_*.json"):
            alert = Alert.model_validate_json(path.read_bytes())
            if path.name != f"alert_{alert.alert_id}.json":
                raise ValueError("alert_filename_mismatch")
            self.history.stage(
                alert, self.history.basis(alert.alert_id) or "", projected=True
            )
            count += 1
        return count

    def retire_files(self) -> None:
        """Remove only files already committed to the private store."""
        for path in self.config.courier.proposals.glob("alert_*.json"):
            alert = Alert.model_validate_json(path.read_bytes())
            if self.history.body(alert.alert_id) != alert:
                raise ValueError("alert_body_conflict")
            path.unlink()
        if self.config.courier.proposals.exists():
            fsync_directory(self.config.courier.proposals, strict=True)

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

    def republish(self) -> int:
        """HTTP -> files includes acknowledged alerts, with identical IDs/bodies.

        The phone ledger suppresses notifications for already published/dismissed
        IDs and preserves unread bodies. Server acks cannot filter this replay.
        """
        alerts = self.history.bodies()
        for alert in alerts:
            self._publish(alert)
            self.history.projected(alert.alert_id)
        return len(alerts)
