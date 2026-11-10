"""Change alerts for newly approved schedules against earlier approved ones.

Policy:

- Input is the schedule Threads extracted from one approved summary, plus the
  full Fabric snapshot. Fabric is only read; nothing is appended here.
- A new schedule is the same event as an earlier, still active schedule Thread
  when its content is equal (after case and whitespace folding) and, if both
  carry tags, they share at least one tag. The latest such Thread is the
  approved schedule being changed.
- The comparison uses the ``schedule`` value ``{start, end, timezone}``: a
  different start instant or end instant is a change. The timezone alone is not.
- Window: the approved schedule must not be past, and it or the new time must
  lie in ``[now, now + horizon)``.
- At most ``max_alerts`` per package over its lifetime, earliest event first.
- The message names only the local date and what changed, never titles, names or
  excerpts.
- ``alert_id`` is derived from (package, new Thread, approved Thread, both
  canonical values), so a replay of the same approval is not re-warned.
  ``AlertHistory`` remembers published IDs, so phone deletions are not recreated.
"""

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from weaver.config import ServerConfig
from weaver.schema import Alert, ScheduleValue, Thread

from .history import AlertHistory

logger = logging.getLogger(__name__)

MESSAGE = (
    "{month}월 {day}일 일정의 시각이 바뀌었을 수 있음. 새로 승인된 요약과 다릅니다."
)
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def alert_id_for(package_id: str, thread_id: str, signature: str) -> str:
    """Deterministic ULID-shaped ID (128 hash bits, so the first digit is <= 7)."""
    digest = hashlib.sha256(
        "\x1f".join((package_id, thread_id, signature)).encode()
    ).digest()
    value = int.from_bytes(digest[:16], "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "alt_" + "".join(reversed(chars))


def _title(thread: Thread) -> str:
    return " ".join((thread.content or "").casefold().split())


def _schedule(thread: Thread) -> ScheduleValue | None:
    if thread.kind != "일정" or thread.field != "schedule" or thread.value is None:
        return None
    try:
        return ScheduleValue.model_validate(thread.value)
    except ValueError:
        return None


def _key(value: ScheduleValue) -> str:
    end = "" if value.end is None else value.end.isoformat()
    return f"{value.start.isoformat()}/{end}"


@dataclass(frozen=True)
class Pending:
    package_id: str
    thread_id: str
    basis: str


class ScheduleAlertDetector:
    def __init__(
        self,
        history: AlertHistory,
        *,
        timezone: ZoneInfo,
        clock: Callable[[], datetime] | None = None,
        horizon: timedelta = timedelta(days=30),
        max_alerts: int = 3,
    ) -> None:
        self.history = history
        self.zone = timezone
        self.clock = clock or (lambda: datetime.now(timezone))
        self.horizon = horizon
        self.max_alerts = max_alerts
        self._pending: dict[str, Pending] = {}

    @classmethod
    def from_config(cls, config: ServerConfig) -> "ScheduleAlertDetector":
        return cls(
            AlertHistory(config.state_root / "alerts.sqlite3"),
            timezone=config.time.zone,
        )

    def detect(
        self, package_id: str, selected: Sequence[Thread], snapshot: Sequence[Thread]
    ) -> list[Alert]:
        """Unpublished alerts for one approval; call ``published`` after writing."""
        now = self.clock()
        retracted = {t.retracts for t in snapshot if t.retracts is not None}
        active = [
            t for t in snapshot if t.thread_id not in retracted and t.retracts is None
        ]
        chosen = {t.thread_id for t in selected}
        found: list[tuple[datetime, Alert, Pending]] = []
        for thread in selected:
            new = _schedule(thread)
            if new is None or thread.thread_id in retracted:
                continue
            before = [
                t
                for t in active
                if t.seq < thread.seq
                and t.thread_id not in chosen
                and self._same_event(t, thread)
            ]
            if not before:
                continue
            approved = max(before, key=lambda t: t.seq)
            old = _schedule(approved)
            if old is None or _key(old) == _key(new):
                continue
            if (old.end or old.start) < now:
                continue
            if not (
                now <= old.start < now + self.horizon
                or now <= new.start < now + self.horizon
            ):
                continue
            signature = f"{_key(old)}>{_key(new)}"
            local = old.start.astimezone(ZoneInfo(old.timezone))
            alert = Alert(
                schema_version=2,
                alert_id=alert_id_for(
                    package_id, thread.thread_id, f"{approved.thread_id}:{signature}"
                ),
                package_id=package_id,
                thread_id=thread.thread_id,
                message=MESSAGE.format(month=local.month, day=local.day),
                created_at=now,
            )
            found.append(
                (
                    old.start,
                    alert,
                    Pending(package_id, thread.thread_id, approved.thread_id),
                )
            )
        found.sort(key=lambda item: item[0])
        fresh = [item for item in found if not self.history.issued(item[1].alert_id)]
        room = max(0, self.max_alerts - self.history.count(package_id))
        alerts = []
        for _, alert, pending in fresh[:room]:
            self._pending[alert.alert_id] = pending
            alerts.append(alert)
        return alerts

    def published(self, alert: Alert) -> None:
        pending = self._pending.pop(alert.alert_id, None)
        if pending is not None:
            self.history.record(alert, pending.basis)

    @staticmethod
    def _same_event(earlier: Thread, later: Thread) -> bool:
        if _schedule(earlier) is None or _title(earlier) != _title(later):
            return False
        if earlier.tags and later.tags:
            return bool(set(earlier.tags) & set(later.tags))
        return True
