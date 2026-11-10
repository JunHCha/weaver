"""Change alerts for pending proposals against approved Fabric state (#9).

Policy (see the PR for alternatives):

- Input is a pending Proposal at publication time (r1 and every revised rev).
  Drafts are never appended to Fabric here; Fabric is only read.
- A proposal Schedule is related to the existing ``ent_`` IDs on it or on its
  일정 draft. New (unmatched) entities have no approved schedule to change.
- The approved schedule of such an entity is its current Fabric value of
  ``date``/``start``/``deadline``/``due_at`` (first present). Without one, or
  with a cancelled/done ``status``, there is nothing to warn about.
- Signals: a draft of the proposal on that entity whose watched field
  (time fields, ``assignee(s)``, ``status``) contradicts the current value; and,
  for a ``meeting`` entity, the Schedule ``start`` against its ``date``/``start``.
  No existing value, or an equal value (same instant, same all-day date, JSON
  equality otherwise), is not a change; so once approved it is never re-warned.
  A time on the approved all-day date only refines it and is not a change.
  Entity types come from proposal.entities: Proposal validation rejects any
  ent_ reference not listed there, so every related entity has a type.
- Window: the approved schedule must not be past (all-day: until local
  midnight), and it or the proposed time must lie in ``[now, now+horizon)``.
- At most ``max_alerts`` per proposal_id over its lifetime, earliest first.
- The message names only the local date and changed aspects, never titles,
  names, values or excerpts, and always says it is pre-approval information.
- alert_id is derived from (proposal_id, schedule ref, change signature of
  approved and proposed canonical values, not Thread IDs), not rev: a revision with the same change is not re-warned, a different change is.
  AlertHistory remembers published IDs, so phone deletions are not recreated.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from weaver.config import ServerConfig
from weaver.fabric.state import InvalidSetOperation, _equal
from weaver.fabric.store import Fabric
from weaver.schema import Alert, EntityState, Proposal
from weaver.schema.models import TIMESTAMP_PATTERN

from .history import AlertHistory

logger = logging.getLogger(__name__)

TIME_FIELDS = ("date", "start", "deadline", "due_at")
EVENT_TIME_FIELDS = ("date", "start")
LABELS = {
    "date": "시각",
    "start": "시각",
    "deadline": "마감",
    "due_at": "마감",
    "assignee": "담당자",
    "assignees": "담당자",
    "status": "상태",
}
LABEL_ORDER = ("시각", "마감", "담당자", "상태")
# Subject particle after the last label: 이 after a final consonant, else 가.
PARTICLE = {"시각": "이", "마감": "이", "담당자": "가", "상태": "가"}
TERMINAL_STATUS = {"취소", "완료", "cancelled", "canceled", "done", "completed"}
NOTICE = "승인 전 정보입니다."
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def alert_id_for(proposal_id: str, schedule_ref: str, signature: str) -> str:
    """Deterministic ULID-shaped ID (128 hash bits, so the first digit is <= 7)."""
    digest = hashlib.sha256(
        "\x1f".join((proposal_id, schedule_ref, signature)).encode()
    ).digest()
    value = int.from_bytes(digest[:16], "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "alt_" + "".join(reversed(chars))


@dataclass(frozen=True)
class Moment:
    """A point in time, or a local all-day date as its [start, end) span."""

    start: datetime
    end: datetime
    all_day: bool
    local_date: date

    def key(self) -> str:
        if self.all_day:
            return "day:" + self.local_date.isoformat()
        return "at:" + self.start.astimezone(UTC).isoformat()


def _moment(value: object, zone: ZoneInfo) -> Moment | None:
    if isinstance(value, datetime):
        value = value.isoformat()
    if not isinstance(value, str):
        return None
    if _DATE.fullmatch(value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
        start = datetime.combine(day, time(), zone)
        end = datetime.combine(day + timedelta(days=1), time(), zone)
        return Moment(start, end, True, day)
    if re.fullmatch(TIMESTAMP_PATTERN, value):
        try:
            at = datetime.fromisoformat(value)
        except ValueError:
            return None
        return Moment(at, at, False, at.astimezone(zone).date())
    return None


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class Change:
    entity_id: str
    field: str
    basis: str  # entry_id of the approved value (history only, not the ID)
    old: str  # canonical approved value
    new: str  # canonical proposed value
    proposed: Moment | None


@dataclass(frozen=True)
class Pending:
    rev: int
    basis: list[str]


class ScheduleAlertDetector:
    def __init__(
        self,
        fabric: Fabric,
        history: AlertHistory,
        *,
        timezone: ZoneInfo,
        clock: Callable[[], datetime] | None = None,
        horizon: timedelta = timedelta(days=30),
        max_alerts: int = 3,
    ) -> None:
        self.fabric = fabric
        self.history = history
        self.zone = timezone
        self.clock = clock or (lambda: datetime.now(timezone))
        self.horizon = horizon
        self.max_alerts = max_alerts
        self._pending: dict[str, Pending] = {}

    @classmethod
    def from_config(
        cls, config: ServerConfig, fabric: Fabric
    ) -> "ScheduleAlertDetector":
        return cls(
            fabric,
            AlertHistory(config.state_root / "alerts.sqlite3"),
            timezone=config.time.zone,
        )

    # ---- public boundary ---------------------------------------------------

    def detect(self, proposal: Proposal) -> list[Alert]:
        """Unpublished alerts for ``proposal``; call ``published`` after writing."""
        now = self.clock()
        candidates: list[tuple[datetime, Alert, list[str]]] = []
        for schedule in proposal.schedules:
            found = self._schedule(proposal, schedule.ref, now)
            if found is None:
                continue
            anchor, changes = found
            signature = json.dumps(
                sorted([c.entity_id, c.field, c.old, c.new] for c in changes)
            )
            labels = {LABELS[c.field] for c in changes}
            ordered = [label for label in LABEL_ORDER if label in labels]
            local = anchor.local_date
            message = (
                f"{local.month}월 {local.day}일 일정의 {'·'.join(ordered)}"
                f"{PARTICLE[ordered[-1]]} 바뀔 수 있음. {NOTICE}"
            )
            alert = Alert(
                schema_version=1,
                alert_id=alert_id_for(proposal.proposal_id, schedule.ref, signature),
                proposal_id=proposal.proposal_id,
                schedule_ref=schedule.ref,
                message=message,
                created_at=now,
            )
            candidates.append((anchor.start, alert, sorted({c.basis for c in changes})))
        candidates.sort(key=lambda item: item[0])
        fresh = [c for c in candidates if not self.history.issued(c[1].alert_id)]
        room = max(0, self.max_alerts - self.history.count(proposal.proposal_id))
        alerts = []
        for _, alert, basis in fresh[:room]:
            self._pending[alert.alert_id] = Pending(proposal.rev, basis)
            alerts.append(alert)
        return alerts

    def published(self, alert: Alert) -> None:
        pending = self._pending.pop(alert.alert_id, None)
        if pending is not None:
            self.history.record(alert, pending.rev, pending.basis)

    # ---- policy ------------------------------------------------------------

    def _schedule(
        self, proposal: Proposal, ref: str, now: datetime
    ) -> tuple[Moment, list[Change]] | None:
        schedule = next(s for s in proposal.schedules if s.ref == ref)
        drafts = {d.ref: d for d in proposal.entry_drafts}
        ids = {e.ref: e.entity_id for e in proposal.entities}
        types = {e.entity_id: e.type for e in proposal.entities if e.entity_id}

        def resolve(refs: list[str]) -> list[str]:
            out = []
            for item in refs:
                entity = item if item.startswith("ent_") else ids.get(item)
                if entity is not None and entity not in out:
                    out.append(entity)
            return out

        draft = drafts[schedule.entry_ref]
        limit = now + self.horizon
        best: Moment | None = None
        changes: list[Change] = []
        for entity in resolve([*schedule.entities, *draft.entities]):
            try:
                state = self.fabric.state(entity)
            except InvalidSetOperation:
                logger.warning("alert skipped entity with invalid assignees state")
                continue
            anchor = self._anchor(state)
            if anchor is None or self._terminal(state):
                continue
            found: list[Change] = []
            touched: set[str] = set()
            for item in proposal.entry_drafts:
                if (
                    item.kind == "요청"
                    or item.field not in LABELS
                    or entity not in resolve(item.entities)
                ):
                    continue
                touched.add(item.field)
                change = self._compare(state, entity, item.field, item.value)
                if change is not None:
                    found.append(change)
            if types.get(entity) == "meeting" and not touched & set(EVENT_TIME_FIELDS):
                for name in EVENT_TIME_FIELDS:
                    current = state.fields.get(name)
                    if current is not None and current.value is not None:
                        change = self._compare(
                            state, entity, name, schedule.start.isoformat()
                        )
                        if change is not None:
                            found.append(change)
                        break
            if not found or not self._future(anchor, now):
                continue
            soon = anchor.start < limit or any(
                c.proposed is not None
                and self._future(c.proposed, now)
                and c.proposed.start < limit
                for c in found
            )
            if not soon:
                continue
            changes.extend(found)
            if best is None or anchor.start < best.start:
                best = anchor
        if best is None:
            return None
        return best, changes

    def _anchor(self, state: EntityState) -> Moment | None:
        for name in TIME_FIELDS:
            current = state.fields.get(name)
            if current is not None:
                moment = _moment(current.value, self.zone)
                if moment is not None:
                    return moment
        return None

    @staticmethod
    def _terminal(state: EntityState) -> bool:
        status = state.fields.get("status")
        return (
            status is not None
            and isinstance(status.value, str)
            and status.value.strip().lower() in TERMINAL_STATUS
        )

    @staticmethod
    def _future(moment: Moment, now: datetime) -> bool:
        return moment.end > now if moment.all_day else moment.start >= now

    def _compare(
        self, state: EntityState, entity: str, field: str, value: JsonValue
    ) -> Change | None:
        if field == "assignees":
            return self._compare_set(state, entity, value)
        current = state.fields.get(field)
        if current is None or current.value is None:
            return None  # nothing approved to contradict
        old = _moment(current.value, self.zone)
        new = _moment(value, self.zone)
        if old is not None and new is not None:
            if old.key() == new.key():
                return None
            if old.all_day and not new.all_day and old.local_date == new.local_date:
                return None  # a time on the approved all-day date only refines it
            canonical, previous = new.key(), old.key()
        else:
            if _equal(current.value, value):
                return None
            canonical = new.key() if new else _json(value)
            previous = old.key() if old else _json(current.value)
        return Change(entity, field, current.entry_id, previous, canonical, new)

    @staticmethod
    def _compare_set(
        state: EntityState, entity: str, value: JsonValue
    ) -> Change | None:
        current = state.sets.get("assignees")
        if current is None or not current.members:
            return None
        members = [m.value for m in current.members]

        def has(item: JsonValue) -> bool:
            return any(_equal(member, item) for member in members)

        if value is None:
            changed = True
        elif isinstance(value, dict) and value.keys() == {"add", "remove"}:
            add, remove = value["add"], value["remove"]
            if not isinstance(add, list) or not isinstance(remove, list):
                return None
            changed = any(not has(a) for a in add if a not in remove) or any(
                has(r) for r in remove
            )
        else:
            return None
        if not changed:
            return None
        previous = _json(sorted(_json(m) for m in members))
        return Change(
            entity, "assignees", current.entry_id, previous, _json(value), None
        )
