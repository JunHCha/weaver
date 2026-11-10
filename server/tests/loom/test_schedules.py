"""Approved schedules reflect the current request state."""

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

    withdrawn = thread(6, retracts="2")
    assert approved_schedules("package", "a" * 64, [schedule], [schedule, withdrawn])[
        0
    ].retracted

    retract_request = thread(5, retracts="1")
    assert (
        approved_schedules(
            "package", "a" * 64, [schedule], [request, schedule, retract_request]
        )[0].blocked_by
        is None
    )


def test_schedule_files_are_replaced_and_retracted(tmp_path):
    from weaver.loom.schedules import write_approved_schedules
    from weaver.schema import ApprovedSchedule

    schedule = ApprovedSchedule(
        schema_version=2,
        revision=1,
        retracted=False,
        package_id="package",
        summary_id="a" * 64,
        thread_id="thread-1",
        title="회의",
        start="2026-01-01T09:00:00+09:00",
        end=None,
        timezone="Asia/Seoul",
        blocked_by="request-1",
    )
    write_approved_schedules(tmp_path, [schedule])
    path = tmp_path / "proposals/schedules/thread-1.json"
    assert ApprovedSchedule.model_validate_json(path.read_text()) == schedule
    write_approved_schedules(tmp_path, [schedule])
    assert ApprovedSchedule.model_validate_json(path.read_text()).revision == 1
    changed = schedule.model_copy(update={"revision": 2, "blocked_by": None})
    write_approved_schedules(tmp_path, [changed])
    assert ApprovedSchedule.model_validate_json(path.read_text()) == changed
    withdrawn = changed.model_copy(update={"revision": 3, "retracted": True})
    write_approved_schedules(tmp_path, [withdrawn])
    assert ApprovedSchedule.model_validate_json(path.read_text()) == withdrawn
    assert list(path.parent.iterdir()) == [path]


def test_failed_replace_preserves_previous_file(tmp_path, monkeypatch):
    import os

    import pytest

    from weaver.loom.schedules import write_approved_schedules
    from weaver.schema import ApprovedSchedule

    schedule = ApprovedSchedule(
        schema_version=2,
        package_id="package",
        summary_id="a" * 64,
        thread_id="thread-1",
        title="회의",
        start="2026-01-01T09:00:00+09:00",
        end=None,
        timezone="Asia/Seoul",
        blocked_by="request-1",
    )
    write_approved_schedules(tmp_path, [schedule])
    path = tmp_path / "proposals/schedules/thread-1.json"
    previous = path.read_bytes()

    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_approved_schedules(
            tmp_path, [schedule.model_copy(update={"blocked_by": None})]
        )
    assert path.read_bytes() == previous
    assert list(path.parent.iterdir()) == [path]
