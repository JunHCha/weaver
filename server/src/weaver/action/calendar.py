"""Turn the judged schedules of a Fabric snapshot into calendar Actions.

``loom.schedules.approved_schedules`` decides which schedules stand (blocked by
an open request, retracted, resolved). This module only maps that outcome to
commands:

- a new schedule becomes ``calendar.register``;
- a schedule that names an earlier one in ``updates`` and carries another value
  becomes ``calendar.update`` of that event;
- a retraction of every live schedule of an event becomes ``calendar.remove``.

An event is a chain of schedule Threads linked by ``updates``. Titles and tags
are never compared: without the explicit link a schedule is always a new event.

A blocked schedule is held, never executed. Blocking does not remove an event
the phone already created, as before. Actions of one event are numbered; the
key ``<kind>:<lineage>:<position>`` makes each logical operation unique.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from weaver.loom.schedules import ApprovedSchedule, approved_schedules
from weaver.schema import CalendarPayload, Thread
from weaver.schema.models import ActionKind

from .outbox import ActionRecord, NewAction, execution_thread_id


def lineages(judged: list[ApprovedSchedule]) -> dict[str, list[ApprovedSchedule]]:
    """Group schedules by event, keyed by the root Thread, in sequence order.

    A schedule joins another's event only when it names that schedule in
    ``updates`` (the Loom approval records this when it says the schedule is a
    change of an existing one). Otherwise it is a new event, however alike the
    titles and tags are.
    """
    groups: dict[str, list[ApprovedSchedule]] = {}
    owner: dict[str, str] = {}
    for item in sorted(judged, key=lambda s: s.thread.seq):
        root = owner.get(item.thread.updates or "", item.thread.thread_id)
        owner[item.thread.thread_id] = root
        groups.setdefault(root, []).append(item)
    return groups


def payload_of(item: ApprovedSchedule) -> CalendarPayload:
    return CalendarPayload(
        title=item.title,
        start=item.value.start,
        end=item.value.end,
        timezone=item.value.timezone,
    )


@dataclass(frozen=True)
class _Intent:
    present: bool
    payload: CalendarPayload | None
    last: ActionRecord | None  # the latest live (not failed or skipped) Action


def _intent(records: list[ActionRecord]) -> _Intent:
    present, payload, last = False, None, None
    for record in records:
        if record.status in ("failed", "skipped"):
            continue
        last = record
        if record.kind == "calendar.remove":
            present = False
        else:
            present, payload = True, record.payload
    return _Intent(present, payload, last)


def derive_calendar_actions(
    snapshot: list[Thread],
    history: Callable[[str], list[ActionRecord]],
    *,
    baseline: int = 0,
    executions: Mapping[str, Thread] | None = None,
) -> list[NewAction]:
    """Actions still needed for the complete business snapshot.

    ``history(lineage)`` returns the Actions already in the outbox for a
    lineage. ``baseline`` hides schedules at or below that Fabric sequence.
    ``executions`` maps a ``실행`` Thread id to the Thread, so an Action that was
    already executed is enqueued as finished instead of running twice.
    """
    selected = [t for t in snapshot if t.kind == "일정" and t.seq > baseline]
    judged = approved_schedules(selected, snapshot)
    needed: list[NewAction] = []
    for root, members in lineages(judged).items():
        records = history(root)
        intent = _intent(records)
        live = [m for m in members if not m.retracted]
        standing = [m for m in live if m.blocked_by is None]
        operation: NewAction | None = None
        failed = records[-1] if records and records[-1].status == "failed" else None
        position = len(records)
        if standing:
            item = standing[-1]
            payload = payload_of(item)
            kind: ActionKind = (
                "calendar.update" if intent.present else "calendar.register"
            )
            latched = (
                failed is not None and failed.kind == kind and failed.payload == payload
            )
            if (not intent.present or intent.payload != payload) and not latched:
                operation = NewAction(
                    kind,
                    item.thread.thread_id,
                    item.thread.seq,
                    item.thread.source_id,
                    payload,
                    root,
                    position,
                )
        elif not live and intent.present and intent.last and intent.payload:
            origin = intent.last
            operation = NewAction(
                "calendar.remove",
                origin.thread_id,
                origin.seq,
                origin.source_id,
                intent.payload,
                root,
                position,
            )
        if operation is not None:
            needed.append(_recorded(operation, executions or {}))
    return needed


def _recorded(item: NewAction, executions: Mapping[str, Thread]) -> NewAction:
    """Mark the Action finished when its 실행 Thread already exists."""
    thread = executions.get(execution_thread_id(item.action_id))
    if thread is None or not isinstance(thread.value, dict):
        return item
    value = thread.value
    external_id = value.get("external_id")
    error = value.get("error")
    return NewAction(
        item.kind,
        item.thread_id,
        item.seq,
        item.source_id,
        item.payload,
        item.lineage,
        item.position,
        "completed" if value.get("outcome") == "completed" else "failed",
        1,
        error if isinstance(error, str) else None,
        external_id if isinstance(external_id, str) else None,
    )
