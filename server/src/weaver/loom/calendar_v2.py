"""Project approved schedule files from a complete Fabric snapshot."""

from weaver.schema.v2 import ApprovedSchedule, ScheduleValue, Thread


def approved_schedules(
    package_id: str, summary_id: str, selected: list[Thread], snapshot: list[Thread]
) -> list[ApprovedSchedule]:
    """Build schedules, retaining a block unless an active decision resolves it.

    The caller supplies the threads extracted from the approved summary and the
    complete current snapshot, including requests and retractions.
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
        if thread.thread_id in retracted or thread.retracts is not None:
            continue
        value = ScheduleValue.model_validate(thread.value)
        result.append(
            ApprovedSchedule(
                schema_version=2,
                package_id=package_id,
                summary_id=summary_id,
                thread_id=thread.thread_id,
                title=thread.content or "",
                start=value.start,
                end=value.end,
                timezone=value.timezone,
                blocked_by=thread.blocked_by
                if thread.blocked_by in open_requests
                else None,
            )
        )
    return result
