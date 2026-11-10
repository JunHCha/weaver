"""Project approved schedule files from a complete Fabric snapshot."""

import os
import tempfile
from pathlib import Path

from weaver.schema import ApprovedSchedule, ScheduleValue, Thread


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
        if thread.retracts is not None:
            continue
        value = ScheduleValue.model_validate(thread.value)
        result.append(
            ApprovedSchedule(
                schema_version=2,
                retracted=thread.thread_id in retracted,
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


def write_approved_schedules(root: Path, schedules: list[ApprovedSchedule]) -> None:
    """Atomically replace each schedule file under the Courier root.

    Revisions increase when the content changes; retractions remain as tombstones.
    """
    directory = root / "proposals" / "schedules"
    directory.mkdir(parents=True, exist_ok=True)
    for schedule in schedules:
        name = schedule.thread_id
        if (
            name in {".", ".."}
            or any(c in name for c in "/\\")
            or any(ord(c) < 32 for c in name)
        ):
            raise ValueError("unsafe thread_id")
        target = directory / f"{name}.json"
        if target.exists():
            previous = ApprovedSchedule.model_validate_json(
                target.read_text(encoding="utf-8")
            )
            if previous.thread_id != name:
                raise ValueError("schedule thread_id mismatch")
            if previous.model_dump(exclude={"revision"}) == schedule.model_dump(
                exclude={"revision"}
            ):
                continue
            schedule = schedule.model_copy(update={"revision": previous.revision + 1})
        else:
            schedule = schedule.model_copy(update={"revision": 1})
        fd, temporary = tempfile.mkstemp(
            prefix=".schedule-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(schedule.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
