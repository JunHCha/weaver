"""Local capture listener and shared spool limits (ADR-044)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class OutboxLimits:
    max_items: int = 10_000
    max_bytes: int = 2 * 1024**3
    min_free_bytes: int = 5 * 1024**3

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.max_items, self.max_bytes, self.min_free_bytes)
        ):
            raise ValueError("outbox_limits_must_be_positive_integers")


@dataclass(frozen=True)
class CourierSettings:
    bind: str = "127.0.0.1"
    port: int = 8788
    max_items: int = 10_000
    max_bytes: int = 2 * 1024**3
    min_free_bytes: int = 5 * 1024**3

    def __post_init__(self) -> None:
        if self.bind not in ("127.0.0.1", "::1"):
            raise ValueError("shepherds.courier.bind: explicit loopback required")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("shepherds.courier.port: invalid port")
        _ = self.limits

    @property
    def limits(self) -> OutboxLimits:
        return OutboxLimits(self.max_items, self.max_bytes, self.min_free_bytes)
