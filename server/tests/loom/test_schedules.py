"""Judged schedules reflect the current request state."""

from weaver.loom.schedules import approved_schedules
from weaver.schema import Thread


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
    result = approved_schedules([schedule], [request, schedule])
    assert result[0].blocked_by == "1"
    assert result[0].value.timezone == "Asia/Seoul"
    decision = thread(3, kind="결정", resolves="1")
    assert (
        approved_schedules([schedule], [request, schedule, decision])[0].blocked_by
        is None
    )
    retract = thread(4, retracts="3")
    assert (
        approved_schedules([schedule], [request, schedule, decision, retract])[
            0
        ].blocked_by
        == "1"
    )

    withdrawn = thread(6, retracts="2")
    assert approved_schedules([schedule], [schedule, withdrawn])[0].retracted

    retract_request = thread(5, retracts="1")
    assert (
        approved_schedules([schedule], [request, schedule, retract_request])[
            0
        ].blocked_by
        is None
    )


def test_title_validation():
    from weaver.loom.schedules import valid_title

    assert valid_title("ACME 서버 정기점검 방문") == "ACME 서버 정기점검 방문"
    for bad in (
        "12월 3일 UPS 점검",
        "UPS 점검 14:00",
        "2026-12 점검",
        "오후 미팅",
        "가" * 31,
        "  ",
        None,
    ):
        assert valid_title(bad) is None
    assert valid_title("가" * 30) == "가" * 30
    assert valid_title("  UPS 점검  ") == "UPS 점검"
    assert valid_title("2시간 교육") == "2시간 교육"


def test_schedule_title_falls_back_to_content():
    value = {
        "start": "2026-01-01T09:00:00+09:00",
        "end": None,
        "timezone": "Asia/Seoul",
        "title": "12월 3일 점검",
    }
    thread = Thread.model_validate(
        dict(
            seq=1,
            thread_id="1",
            kind="일정",
            content="UPS 점검 일정",
            occurred_at="2026-01-01T00:00:00Z",
            recorded_at="2026-01-01T00:00:00Z",
            tags=[],
            field="schedule",
            value=value,
            source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            actor="ai_approved",
            direction=None,
            resolves=None,
            retracts=None,
            correction=None,
        )
    )
    (item,) = approved_schedules([thread], [thread])
    assert item.title == "UPS 점검 일정"
    good = thread.model_copy(update={"value": value | {"title": "서버 정기점검"}})
    assert approved_schedules([good], [good])[0].title == "서버 정기점검"
