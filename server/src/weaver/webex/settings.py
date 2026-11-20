"""Non-secret settings for the ADR-047 Webex pilot."""

from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weaver.shepherd.settings import CourierSettings


@dataclass(frozen=True)
class WebexSettings:
    enabled: bool = False
    instance_id: str = "mac-webex-primary"
    auth: str = "integration_oauth"
    credentials_file: Path | None = None
    courier_token_file: Path | None = None
    exclude_space_ids: tuple[str, ...] = ()
    poll_interval_seconds: int = 300
    initial_backfill_days: int = 7
    reconcile_overlap_days: int = 7
    bundle: str = "space_day"
    timezone: str = "Asia/Seoul"
    attachments: str = "metadata_only"

    def __post_init__(self) -> None:
        from weaver.config import ConfigError

        if type(self.enabled) is not bool:
            raise ConfigError("shepherds.webex.enabled: expected boolean")
        for name in ("instance_id", "timezone"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"shepherds.webex.{name}: expected nonempty string")
        for name, expected in (
            ("auth", "integration_oauth"),
            ("bundle", "space_day"),
            ("attachments", "metadata_only"),
        ):
            if getattr(self, name) != expected:
                raise ConfigError(f"shepherds.webex.{name}: unsupported setting")
        for name in (
            "poll_interval_seconds",
            "initial_backfill_days",
            "reconcile_overlap_days",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ConfigError(f"shepherds.webex.{name}: expected positive integer")
        if not isinstance(self.exclude_space_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.exclude_space_ids
        ):
            raise ConfigError(
                "shepherds.webex.exclude_space_ids: expected string array"
            )
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError("shepherds.webex.timezone: invalid timezone") from None
        if self.enabled and (
            self.credentials_file is None or self.courier_token_file is None
        ):
            raise ConfigError(
                "shepherds.webex: enabled requires both secret file paths"
            )


@dataclass(frozen=True)
class ShepherdSettings:
    webex: WebexSettings = field(default_factory=WebexSettings)
    courier: CourierSettings = field(default_factory=CourierSettings)
