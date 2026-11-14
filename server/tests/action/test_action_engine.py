"""The engine derives calendar Actions from Fabric; replays never duplicate them."""

import sqlite3

import pytest

from weaver.action import ActionOutbox, NewAction
from weaver.schema import CalendarPayload

from .action_helpers import (
    Rig,
    decision,
    request,
    retraction,
    schedule,
)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_new_schedule_becomes_one_register_action(rig):
    rig.add(schedule("s1"))
    assert rig.engine.run() == 1
    (action,) = rig.actions()
    assert action.kind == "calendar.register"
    assert (action.thread_id, action.seq, action.input_version) == ("s1", 1, 1)
    assert action.status == "pending" and action.attempts == 0
    assert action.next_attempt_at is None and action.external_id is None
    assert action.payload.title == "주간 회의"
    assert action.payload.timezone == "Asia/Seoul"
    assert action.action_id.startswith("act_") and len(action.action_id) == 36


def test_replay_and_rewound_cursor_never_enqueue_twice(rig):
    rig.add(schedule("s1"), schedule("s2", title="다른 일정", tags=("김철수",)))
    assert rig.engine.run() == 2
    first = {a.action_id for a in rig.actions()}
    assert rig.engine.run() == 0
    rig.outbox.reset_cursor()
    assert rig.engine.run() == 0
    rig.add(request("r1"))  # an unrelated row moves the cursor but adds nothing
    assert rig.engine.run() == 0
    assert {a.action_id for a in rig.actions()} == first


def test_cursor_and_actions_survive_a_restart(rig):
    rig.add(schedule("s1"))
    rig.engine.run()
    rig.open()
    assert rig.outbox.cursor() == 1
    assert rig.engine.run() == 0
    rig.add(schedule("s2", title="다른 일정", tags=("김철수",)))
    rig.open()
    assert rig.engine.run() == 1
    assert [a.thread_id for a in rig.actions()] == ["s1", "s2"]


def test_a_crash_before_the_cursor_commit_leaves_nothing_half_done(rig):
    payload = CalendarPayload.model_validate(
        {
            "start": "2026-12-01T10:00:00+09:00",
            "end": None,
            "timezone": "Asia/Seoul",
            "title": "회의",
        }
    )
    same_slot = [
        NewAction("calendar.register", "a", 1, "src", payload, "root", 0),
        NewAction("calendar.update", "b", 2, "src", payload, "root", 0),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        rig.outbox.commit(same_slot, 9)
    assert rig.outbox.all() == [] and rig.outbox.cursor() == 0
    rig.add(schedule("s1"))
    assert rig.engine.run() == 1  # the next run still sees the unprocessed rows


def test_blocked_schedule_waits_until_its_request_is_resolved(rig):
    rig.add(request("r1"), schedule("s1", blocked_by="r1"))
    assert rig.engine.run() == 0
    rig.add(decision("d1", "r1"))
    assert rig.engine.run() == 1
    assert rig.kinds() == ["calendar.register"]
    # Reopening the request holds later changes but does not remove the event.
    rig.add(retraction("x1", "d1"))
    assert rig.engine.run() == 0
    assert rig.kinds() == ["calendar.register"]


def test_blocked_update_is_held_until_resolved(rig):
    rig.add(schedule("s1"))
    rig.engine.run()
    rig.add(
        request("r1"),
        schedule(
            "s2", start="2026-12-02T10:00:00+09:00", blocked_by="r1", updates="s1"
        ),
    )
    assert rig.engine.run() == 0
    rig.add(decision("d1", "r1"))
    assert rig.engine.run() == 1
    assert rig.kinds() == ["calendar.register", "calendar.update"]


def test_retracted_before_registration_creates_nothing(rig):
    rig.add(schedule("s1"), retraction("x1", "s1"))
    assert rig.engine.run() == 0


def test_retracting_a_registered_schedule_becomes_remove(rig):
    rig.add(schedule("s1"))
    rig.engine.run()
    register = rig.actions()[0]
    rig.add(retraction("x1", "s1"))
    assert rig.engine.run() == 1
    remove = rig.actions()[1]
    assert remove.kind == "calendar.remove"
    assert (remove.thread_id, remove.seq) == ("s1", 1)
    assert remove.payload == register.payload
    assert rig.engine.run() == 0
    rig.outbox.reset_cursor()
    assert rig.engine.run() == 0


def test_changed_time_becomes_update_not_a_second_event(rig):
    rig.add(schedule("s1"))
    rig.engine.run()
    rig.add(schedule("s2", start="2026-12-03T10:00:00+09:00", updates="s1"))
    assert rig.engine.run() == 1
    register, update = rig.actions()
    assert update.kind == "calendar.update" and update.thread_id == "s2"
    assert update.input_version == 2
    assert update.payload.start.isoformat() == "2026-12-03T10:00:00+09:00"
    # Saying the same thing again changes nothing.
    rig.add(schedule("s3", start="2026-12-03T10:00:00+09:00", updates="s2"))
    assert rig.engine.run() == 0
    assert register.lineage == update.lineage


def test_retracting_the_change_restores_the_earlier_value(rig):
    rig.add(
        schedule("s1"), schedule("s2", start="2026-12-03T10:00:00+09:00", updates="s1")
    )
    rig.engine.run()
    assert rig.kinds() == ["calendar.register"]  # both arrived in one pass: one event
    assert rig.actions()[0].payload.start.isoformat() == "2026-12-03T10:00:00+09:00"
    rig.add(retraction("x1", "s2"))
    assert rig.engine.run() == 1
    restore = rig.actions()[1]
    assert restore.kind == "calendar.update"
    assert restore.payload.start.isoformat() == "2026-12-01T10:00:00+09:00"


def test_same_title_and_tags_without_a_link_are_two_events(rig):
    rig.add(schedule("s1"))
    rig.add(schedule("s2", start="2026-12-03T10:00:00+09:00"))
    rig.engine.run()
    assert rig.kinds() == ["calendar.register"] * 2
    assert len({a.lineage for a in rig.actions()}) == 2


def test_an_explicit_link_makes_one_update_and_the_event_id_is_kept(rig):
    rig.add(schedule("s1"))
    rig.job.run()
    register = rig.actions()[0]
    rig.report(register)
    rig.job.run()
    rig.add(schedule("s2", start="2026-12-03T10:00:00+09:00", updates="s1"))
    rig.job.run()
    assert rig.kinds() == ["calendar.register", "calendar.update"]
    (action, event) = rig.outbox.dispatchable(0)[0]
    assert action.kind == "calendar.update"
    assert event == "content://calendar/events/7"


def test_a_chain_of_links_stays_one_event(rig):
    rig.add(schedule("s1"))
    rig.add(schedule("s2", start="2026-12-02T10:00:00+09:00", updates="s1"))
    rig.add(schedule("s3", start="2026-12-03T10:00:00+09:00", updates="s2"))
    rig.engine.run()
    assert rig.kinds() == ["calendar.register"]
    assert rig.actions()[0].thread_id == "s3"


def test_actions_of_one_event_run_in_order_with_the_event_id(rig):
    rig.add(schedule("s1"))
    rig.engine.run()
    rig.add(schedule("s2", start="2026-12-03T10:00:00+09:00", updates="s1"))
    rig.engine.run()
    register, update = rig.actions()
    offered = rig.outbox.dispatchable(0)
    assert [(a.action_id, e) for a, e in offered] == [(register.action_id, None)]
    rig.outbox.finish(
        register.action_id,
        outcome="completed",
        external_id="content://calendar/events/7",
        error=None,
        attempts=1,
    )
    offered = rig.outbox.dispatchable(0)
    assert [(a.action_id, e) for a, e in offered] == [
        (update.action_id, "content://calendar/events/7")
    ]


def test_failed_register_skips_followers_and_is_not_retried_forever(rig):
    rig.add(schedule("s1"))
    rig.engine.run()
    rig.add(schedule("s2", start="2026-12-03T10:00:00+09:00", updates="s1"))
    rig.engine.run()
    register, update = rig.actions()
    rig.outbox.finish(
        register.action_id,
        outcome="failed",
        external_id=None,
        error="manual_review",
        attempts=1,
    )
    assert rig.outbox.get(update.action_id).status == "skipped"
    rig.outbox.reset_cursor()
    assert rig.engine.run() == 1  # the surviving intent (s2) gets its own register
    assert rig.actions()[-1].kind == "calendar.register"
    rig.outbox.finish(
        rig.actions()[-1].action_id,
        outcome="failed",
        external_id=None,
        error="manual_review",
        attempts=1,
    )
    rig.outbox.reset_cursor()
    assert rig.engine.run() == 0  # same failed operation: wait for a new intent


def test_schedules_before_the_baseline_are_never_enqueued(rig):
    (rig.root / "proposals" / "schedules").mkdir(parents=True)
    (rig.root / "proposals" / "schedules" / "s1.json").write_text("{}")
    rig.add(schedule("s1"))
    rig.job.run()
    assert rig.actions() == []
    assert not (rig.root / "proposals" / "schedules").exists()  # retired
    rig.add(schedule("s2", title="새 일정", tags=("김철수",)))
    rig.job.run()
    assert [a.thread_id for a in rig.actions()] == ["s2"]
    assert (rig.root / "actions" / f"{rig.actions()[0].action_id}.json").exists()


def test_fresh_install_without_legacy_files_starts_from_the_beginning(rig):
    rig.add(schedule("s1"))
    rig.job.run()
    assert rig.outbox.baseline() == 0
    assert [a.thread_id for a in rig.actions()] == ["s1"]


def test_lost_outbox_is_rebuilt_without_running_finished_actions_again(rig):
    rig.add(schedule("s1"))
    rig.job.run()
    (action,) = rig.actions()
    rig.report(action)
    rig.job.run()
    assert rig.outbox.get(action.action_id).status == "completed"
    (rig.tmp_path / "action-outbox.sqlite3").unlink()
    for suffix in ("-wal", "-shm"):
        (rig.tmp_path / f"action-outbox.sqlite3{suffix}").unlink(missing_ok=True)
    rig.open()
    rig.job.run()
    (rebuilt,) = rig.actions()
    assert rebuilt.action_id == action.action_id
    assert rebuilt.status == "completed"
    assert rebuilt.external_id == "content://calendar/events/7"
    assert rig.outbox.dispatchable(0) == []


def test_outbox_is_private(tmp_path):
    outbox = ActionOutbox(tmp_path / "state" / "action-outbox.sqlite3")
    assert (outbox.path.stat().st_mode & 0o777) == 0o600
