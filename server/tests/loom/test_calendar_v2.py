"""Approved schedules reflect the current request state."""

from weaver.loom.calendar_v2 import approved_schedules
from weaver.schema.v2 import Thread


def test_block_resolution_and_timezone():
    base = dict(
        content="회의",
        occurred_at="2026-01-01T00:00:00Z",
        recorded_at="2026-01-01T00:00:00Z",
        tags=[],
        field=None,
        value=None,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        actor="ai_approved",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )

    def thread(seq, **changes):
        return Thread.model_validate(
            base | {"seq": seq, "thread_id": str(seq), "kind": "기록"} | changes
        )

    request = thread(1, kind="요청", direction="받음")
    schedule = thread(
        2,
        kind="일정",
        field="schedule",
        value={
            "start": "2026-01-01T09:00:00+09:00",
            "end": None,
            "timezone": "Asia/Seoul",
        },
        blocked_by="1",
    )
    result = approved_schedules("package", "a" * 64, [schedule], [request, schedule])
    assert result[0].blocked_by == "1"
    assert result[0].timezone == "Asia/Seoul"
    decision = thread(3, kind="결정", resolves="1")
    assert (
        approved_schedules(
            "package", "a" * 64, [schedule], [request, schedule, decision]
        )[0].blocked_by
        is None
    )
    retract = thread(4, retracts="3")
    assert (
        approved_schedules(
            "package", "a" * 64, [schedule], [request, schedule, decision, retract]
        )[0].blocked_by
        == "1"
    )

    retract_request = thread(5, retracts="1")
    assert (
        approved_schedules(
            "package", "a" * 64, [schedule], [request, schedule, retract_request]
        )[0].blocked_by
        is None
    )
