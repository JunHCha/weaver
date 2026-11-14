"""Judge which approved schedules stand, from a complete Fabric snapshot.

The result feeds the Action engine (``weaver.action``), which turns it into
``calendar.register``, ``calendar.update`` and ``calendar.remove`` commands.
"""

from dataclasses import dataclass

from weaver.schema import ScheduleValue, Thread


@dataclass(frozen=True)
class ApprovedSchedule:
    """One schedule Thread with its current standing."""

    thread: Thread
    value: ScheduleValue
    title: str
    retracted: bool
    blocked_by: str | None


def approved_schedules(
    selected: list[Thread], snapshot: list[Thread]
) -> list[ApprovedSchedule]:
    """Judge schedules, retaining a block unless an active decision resolves it.

    The caller supplies the schedule threads to judge and the complete current
    snapshot, including requests and retractions.
    """
    retracted = {thread.retracts for thread in snapshot if thread.retracts}
    active = [
        t for t in snapshot if t.thread_id not in retracted and t.retracts is None
    ]
    resolved = {t.resolves for t in active if t.resolves}
    open_requests = {t.thread_id for t in active if t.kind == "요청"} - resolved
    result = []
    for thread in selected:
        if thread.kind != "일정" or thread.field != "schedule" or thread.value is None:
            continue
        if thread.retracts is not None:
            continue
        result.append(
            ApprovedSchedule(
                thread=thread,
                value=ScheduleValue.model_validate(thread.value),
                title=thread.content or "",
                retracted=thread.thread_id in retracted,
                blocked_by=thread.blocked_by
                if thread.blocked_by in open_requests
                else None,
            )
        )
    return result
