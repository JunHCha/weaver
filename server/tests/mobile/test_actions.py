"""Action dispatch to the one executor phone and ActionResult over HTTP."""

import json

from weaver.action import ActionJob
from weaver.fabric import ThreadQuery
from weaver.schema import Action

from .helpers import Harness, result, schedule

HTTP = 'transport = "http"\n'


def rig(tmp_path, mobile=HTTP):
    h = Harness(tmp_path, mobile)
    job = ActionJob(h.fabric, h.outbox, h.actions)
    h.fabric.append([schedule("s1")])
    job.run()
    return h, job


def executions(h):
    return h.fabric.entries(ThreadQuery(kind="실행"))


def test_only_the_executor_phone_is_offered_actions_over_http(tmp_path):
    h, _ = rig(tmp_path)
    reader = h.pair(
        "read", "action", label="tablet"
    )  # has the scope, is not the executor
    denied = h.client.get("/mobile/v1/actions", headers=reader)
    assert (denied.status_code, denied.json()["code"]) == (403, "not_executor")
    plain = h.pair("read", label="other")
    assert (
        h.client.get("/mobile/v1/actions", headers=plain).json()["code"]
        == "scope_denied"
    )
    fold = h.pair("read", "action", executor=True, label="fold")
    offered = h.client.get("/mobile/v1/actions", headers=fold)
    assert offered.status_code == 200
    (item,) = offered.json()["items"]
    action = Action.model_validate(item)
    assert action.kind == "calendar.register" and action.external_id is None
    one = h.client.get(f"/mobile/v1/actions/{action.action_id}", headers=fold)
    assert one.json() == item
    assert (
        h.client.get("/mobile/v1/actions/act_" + "0" * 32, headers=fold).status_code
        == 404
    )
    # No Action file is offered to a second executor while HTTP owns dispatch.
    h.actions.publish()
    assert list((h.config.sync_root / "actions").glob("*.json")) == []


def test_files_transport_does_not_dispatch_over_http(tmp_path):
    h, job = rig(tmp_path, mobile="")  # default: files
    fold = h.pair("read", "action", executor=True)
    blocked = h.client.get("/mobile/v1/actions", headers=fold)
    assert (blocked.status_code, blocked.json()["code"]) == (409, "transport_files")
    job.run()
    assert len(list((h.config.sync_root / "actions").glob("*.json"))) == 1


def test_result_is_202_then_one_execution_thread_and_outbox_finished(tmp_path):
    h, _ = rig(tmp_path)
    fold = h.pair("read", "action", executor=True)
    (record,) = h.outbox.all()
    url = f"/mobile/v1/actions/{record.action_id}/result"
    response = h.client.put(url, json=result(record.action_id), headers=fold)
    assert response.status_code == 202
    assert executions(h) == [] and h.outbox.get(record.action_id).status == "pending"
    h.applier.run()
    status = h.client.get(
        f"/mobile/v1/operations/{response.json()['operation_id']}", headers=fold
    )
    assert status.json()["status"] == "applied"
    (thread,) = executions(h)
    assert thread.executes == "s1"
    assert h.outbox.get(record.action_id).status == "completed"
    # Once finished it is no longer offered.
    assert h.client.get("/mobile/v1/actions", headers=fold).json() == {"items": []}


def test_resending_a_result_after_a_lost_response_is_idempotent(tmp_path):
    h, _ = rig(tmp_path)
    fold = h.pair("read", "action", executor=True)
    (record,) = h.outbox.all()
    url = f"/mobile/v1/actions/{record.action_id}/result"
    first = h.client.put(url, json=result(record.action_id), headers=fold)
    again = h.client.put(url, json=result(record.action_id), headers=fold)
    assert again.json()["operation_id"] == first.json()["operation_id"]
    h.applier.run()
    third = h.client.put(
        url, json=result(record.action_id), headers=fold
    )  # after applied
    assert third.status_code == 202 and third.json()["status"] == "applied"
    h.applier.run()
    assert len(executions(h)) == 1


def test_conflicting_or_invalid_results_are_refused(tmp_path):
    h, _ = rig(tmp_path)
    fold = h.pair("read", "action", executor=True)
    (record,) = h.outbox.all()
    url = f"/mobile/v1/actions/{record.action_id}/result"
    h.client.put(url, json=result(record.action_id), headers=fold)
    other = h.client.put(url, json=result(record.action_id, "failed"), headers=fold)
    assert (other.status_code, other.json()["code"]) == (409, "result_conflict")
    assert (
        h.client.put(url, json={"x": 1}, headers=fold).json()["code"]
        == "invalid_result"
    )
    mismatch = h.client.put(url, json=result("act_" + "1" * 32), headers=fold)
    assert mismatch.json()["code"] == "id_mismatch"
    ghost = "act_" + "2" * 32
    unknown = h.client.put(
        f"/mobile/v1/actions/{ghost}/result", json=result(ghost), headers=fold
    )
    assert (unknown.status_code, unknown.json()["code"]) == (404, "unknown_action")


def test_crash_after_the_domain_commit_replays_without_duplicates(tmp_path):
    h, _ = rig(tmp_path)
    fold = h.pair("read", "action", executor=True)
    (record,) = h.outbox.all()
    response = h.client.put(
        f"/mobile/v1/actions/{record.action_id}/result",
        json=result(record.action_id),
        headers=fold,
    )
    h.applier.run()
    # The process died after the domain commit but before the journal update.
    h.store.update_operation(response.json()["operation_id"], "queued")
    with h.store._connect() as db:
        db.execute(
            "UPDATE operations SET payload=? WHERE operation_id=?",
            (json.dumps(result(record.action_id)), response.json()["operation_id"]),
        )
    h.applier.run()
    assert h.store.operation(response.json()["operation_id"]).status == "applied"
    assert len(executions(h)) == 1


def test_file_and_http_results_for_one_action_run_once(tmp_path):
    h, job = rig(tmp_path)
    fold = h.pair("read", "action", executor=True)
    (record,) = h.outbox.all()
    from weaver.schema import ActionResult

    payload = result(record.action_id)
    results = h.config.sync_root / "action-results"
    results.mkdir(exist_ok=True)
    (results / f"{record.action_id}.json").write_text(json.dumps(payload))
    job.run()  # the file result is applied first
    assert len(executions(h)) == 1
    http = h.client.put(
        f"/mobile/v1/actions/{record.action_id}/result", json=payload, headers=fold
    )
    assert http.status_code == 202
    h.applier.run()
    assert h.store.operation(http.json()["operation_id"]).status == "applied"
    assert len(executions(h)) == 1
    # A different late result over HTTP is a failed operation, never a second Thread.
    differing = result(record.action_id, "failed")
    late = h.client.put(
        f"/mobile/v1/actions/{record.action_id}/result", json=differing, headers=fold
    )
    assert late.status_code == 409
    del ActionResult


def test_actions_follow_their_lineage_order_over_http(tmp_path):
    h = Harness(tmp_path, HTTP)
    job = ActionJob(h.fabric, h.outbox, h.actions)
    h.fabric.append([schedule("s1")])
    job.run()
    fold = h.pair("read", "action", executor=True)
    (register,) = h.client.get("/mobile/v1/actions", headers=fold).json()["items"]
    h.fabric.append(
        [
            schedule(
                "s2",
                updates="s1",
                value={
                    "start": "2026-12-02T10:00:00+09:00",
                    "end": None,
                    "timezone": "Asia/Seoul",
                },
            )
        ]
    )
    job.run()
    # The update waits for the register result (it needs the event ID).
    items = h.client.get("/mobile/v1/actions", headers=fold).json()["items"]
    assert [i["kind"] for i in items] == ["calendar.register"]
    h.client.put(
        f"/mobile/v1/actions/{register['action_id']}/result",
        json=result(register["action_id"]),
        headers=fold,
    )
    h.applier.run()
    job.run()
    items = h.client.get("/mobile/v1/actions", headers=fold).json()["items"]
    assert [(i["kind"], i["external_id"]) for i in items] == [
        ("calendar.update", "content://calendar/events/7")
    ]
