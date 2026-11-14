"""Actions go out through Courier files; results come back as 실행 Threads."""

import json
import os

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from weaver.fabric import ThreadQuery
from weaver.schema import Action, Thread

from .action_helpers import ROOT, Rig, retraction, schedule


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def validate(name: str, payload: object) -> None:
    schema = json.loads((ROOT / "schema" / f"{name}.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def executions(rig: Rig) -> list[Thread]:
    return rig.fabric.entries(ThreadQuery(kind="실행"))


def test_dispatchable_actions_are_published_as_schema_valid_files(rig):
    rig.add(schedule("s1"))
    rig.job.run()
    (action,) = rig.actions()
    path = rig.root / "actions" / f"{action.action_id}.json"
    payload = json.loads(path.read_text())
    validate("Action", payload)
    wire = Action.model_validate(payload)
    assert wire.kind == "calendar.register" and wire.external_id is None
    assert [p.name for p in (rig.root / "actions").iterdir()] == [path.name]
    stamp = path.stat().st_mtime_ns
    rig.job.run()  # unchanged content is not rewritten
    assert path.stat().st_mtime_ns == stamp


def test_result_is_recorded_as_one_execution_thread(rig):
    rig.add(schedule("s1", tags=("홍길동",)))
    rig.job.run()
    (action,) = rig.actions()
    result = rig.report(action)
    rig.job.run()
    (thread,) = executions(rig)
    assert thread.kind == "실행" and thread.actor == "system"
    assert thread.executes == "s1" and thread.tags == []
    assert thread.source_id == action.source_id
    assert thread.occurred_at == result.finished_at
    assert thread.value == {
        "action_id": action.action_id,
        "action": "calendar.register",
        "outcome": "completed",
        "external_id": "content://calendar/events/7",
        "error": None,
    }
    validate("Thread", thread.model_dump(mode="json"))
    done = rig.outbox.get(action.action_id)
    assert done.status == "completed" and done.attempts == 1
    assert done.external_id == "content://calendar/events/7"
    assert not (rig.courier.results / f"{action.action_id}.json").exists()
    assert not (rig.root / "actions" / f"{action.action_id}.json").exists()


def test_replaying_a_result_is_idempotent(rig):
    rig.add(schedule("s1"))
    rig.job.run()
    (action,) = rig.actions()
    rig.report(action)
    rig.job.run()
    seq = executions(rig)[0].seq
    rig.report(action)  # the phone reports again after a resync
    assert rig.courier.ingest() == {}
    assert [t.seq for t in executions(rig)] == [seq]
    assert not (rig.courier.results / f"{action.action_id}.json").exists()
    rig.report(action)
    rig.open()
    rig.job.run()
    assert len(executions(rig)) == 1
    assert rig.outbox.get(action.action_id).status == "completed"


def test_crash_between_the_fabric_append_and_the_outbox_update_recovers(
    rig, monkeypatch
):
    rig.add(schedule("s1"))
    rig.job.run()
    (action,) = rig.actions()
    rig.report(action)

    def crash(*args, **kwargs):
        raise RuntimeError("process died")

    with monkeypatch.context() as patch:
        patch.setattr(rig.outbox, "finish", crash)
        with pytest.raises(RuntimeError):
            rig.courier.ingest()
    assert len(executions(rig)) == 1
    assert rig.outbox.get(action.action_id).status == "pending"
    assert (rig.courier.results / f"{action.action_id}.json").exists()
    rig.open()
    rig.job.run()
    assert len(executions(rig)) == 1
    assert rig.outbox.get(action.action_id).status == "completed"


def test_failed_result_is_recorded_and_followers_are_skipped(rig):
    rig.add(schedule("s1"))
    rig.job.run()
    rig.add(schedule("s2", start="2026-12-03T10:00:00+09:00", updates="s1"))
    rig.job.run()
    register, update = rig.actions()
    rig.report(register, "failed", error="manual_review")
    rig.courier.ingest()
    (thread,) = executions(rig)
    assert (
        thread.value["outcome"] == "failed" and thread.value["error"] == "manual_review"
    )
    assert thread.value["external_id"] is None
    assert rig.outbox.get(register.action_id).status == "failed"
    assert rig.outbox.get(register.action_id).error == "manual_review"
    assert rig.outbox.get(update.action_id).status == "skipped"


def test_update_and_remove_are_offered_with_the_created_event(rig):
    rig.add(schedule("s1"))
    rig.job.run()
    rig.report(rig.actions()[0])
    rig.add(schedule("s2", start="2026-12-03T10:00:00+09:00", updates="s1"))
    rig.job.run()
    update = rig.actions()[1]
    wire = Action.model_validate_json(
        (rig.root / "actions" / f"{update.action_id}.json").read_text()
    )
    assert wire.kind == "calendar.update"
    assert wire.external_id == "content://calendar/events/7"
    rig.report(update, external_id="content://calendar/events/7")
    rig.add(retraction("x1", "s1"), retraction("x2", "s2"))
    rig.job.run()
    remove = rig.actions()[2]
    assert remove.kind == "calendar.remove"
    wire = Action.model_validate_json(
        (rig.root / "actions" / f"{remove.action_id}.json").read_text()
    )
    assert wire.external_id == "content://calendar/events/7"
    assert [t.value["action"] for t in executions(rig)] == [
        "calendar.register",
        "calendar.update",
    ]


def test_unknown_conflicting_and_malformed_results_are_left_for_a_human(rig, caplog):
    rig.add(schedule("s1"))
    rig.job.run()
    (action,) = rig.actions()
    rig.report(action)
    rig.job.run()
    # A different outcome for an already recorded Action.
    rig.report(action, "failed", error="provider_error")
    assert rig.courier.ingest() == {f"{action.action_id}.json": "result_conflict"}
    assert len(executions(rig)) == 1
    (rig.courier.results / f"{action.action_id}.json").unlink()
    # An Action this outbox never issued.
    ghost = action.__class__(**{**action.__dict__, "action_id": "act_" + "e" * 32})
    rig.report(ghost)
    assert rig.courier.ingest() == {f"{ghost.action_id}.json": "unknown_action"}
    (rig.courier.results / f"{ghost.action_id}.json").write_text("{not json")
    assert rig.courier.ingest() == {f"{ghost.action_id}.json": "invalid_json"}
    other = "act_" + "d" * 32
    (rig.courier.results / f"{other}.json").write_text(
        (rig.courier.results / f"{ghost.action_id}.json").read_text()
    )
    (rig.courier.results / f"{ghost.action_id}.json").unlink()
    (rig.courier.results / ".act_x.json.0123456789abcdef.tmp").write_text("{")
    assert rig.courier.ingest() == {f"{other}.json": "invalid_json"}
    assert "content" not in caplog.text


def test_finished_action_files_are_removed_and_legacy_files_retired(rig):
    rig.add(schedule("s1"))
    rig.job.run()
    (action,) = rig.actions()
    stale = rig.root / "actions" / f"{action.action_id}.json"
    rig.report(action)
    rig.courier.ingest()
    assert stale.exists()
    rig.courier.publish()
    assert not stale.exists()
    leftover = rig.root / "actions" / f".{action.action_id}.json.0123456789abcdef.tmp"
    leftover.write_text("{")
    rig.courier.publish()
    assert leftover.exists()  # may still be in flight
    os.utime(leftover, (0, 0))
    rig.courier.publish()
    assert not leftover.exists()
    legacy = rig.root / "proposals" / "schedules"
    legacy.mkdir(parents=True)
    (legacy / "old.json").write_text("{}")
    rig.courier.retire_legacy()
    assert not legacy.exists()
