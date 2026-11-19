"""Request-specific effective values and optimistic concurrency revisions."""

from weaver.schema import Thread


def request_values(rows: list[Thread]) -> tuple[dict[str, str | None], dict[str, int]]:
    """Fold active deadline amendments; revisions also reflect resolutions/undo.

    Tag corrections and execution results do not change the business revision.
    Retractions stay revision-relevant even after their target was undone.
    """
    by_id = {t.thread_id: t for t in rows}
    retracted = {t.retracts for t in rows if t.retracts}
    deadlines: dict[str, str | None] = {
        t.thread_id: None for t in rows if t.kind == "요청" and t.retracts is None
    }
    revisions = {key: by_id[key].seq for key in deadlines}
    for t in sorted(rows, key=lambda t: (t.occurred_at, t.recorded_at, t.seq)):
        if (
            t.thread_id in deadlines
            and t.kind == "요청"
            and t.field == "deadline"
            and isinstance(t.value, str)
        ):
            deadlines[t.thread_id] = t.value
        if t.amends and t.thread_id not in retracted:
            deadlines[t.amends] = str(t.value)
        target = t.amends or t.resolves or t.retracts
        if t.retracts:
            original = by_id.get(t.retracts)
            if original:
                target = original.amends or original.resolves or original.thread_id
        if target in revisions:
            revisions[target] = max(revisions[target], t.seq)
    return deadlines, revisions
