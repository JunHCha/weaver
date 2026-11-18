"""Review packages, If-Match verdicts, the 202/operation split and idempotency."""

import json
from pathlib import Path

from .helpers import Harness, verdict

BASE = "/mobile/v1/reviews/pkg_a/revisions"


def etag(h, headers, revision=1):
    return h.client.get(f"{BASE}/{revision}", headers=headers).headers["etag"]


def submit(h, headers, body=None, *, revision=1, match=True):
    send = dict(headers)
    if match is True:
        send["If-Match"] = etag(h, headers, revision)
    elif match:
        send["If-Match"] = match
    return h.client.put(
        f"{BASE}/{revision}/verdict",
        json=body or verdict(revision=revision),
        headers=send,
    )


def operation(h, headers, response):
    return h.client.get(
        f"/mobile/v1/operations/{response.json()['operation_id']}", headers=headers
    ).json()


def test_list_and_get_open_packages_with_etag(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    assert h.client.get("/mobile/v1/reviews", headers=headers).json() == {"items": []}
    h.add_package("pkg_a")
    h.add_package("pkg_a", 2)
    h.add_package("pkg_b")
    items = h.client.get("/mobile/v1/reviews", headers=headers).json()["items"]
    assert [(i["package_id"], i["revision"]) for i in items] == [
        ("pkg_a", 2),
        ("pkg_b", 1),
    ]
    one = h.client.get(f"{BASE}/2", headers=headers)
    assert (
        one.json()["draft_body"] == "초안" and one.headers["etag"] == items[0]["etag"]
    )
    assert (
        h.client.get(f"{BASE}/9", headers=headers).json()["code"] == "package_not_found"
    )


def test_verdict_is_202_then_applied_only_after_domain_confirmation(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    response = submit(h, headers)
    assert response.status_code == 202
    assert response.headers["location"].endswith(response.json()["operation_id"])
    assert operation(h, headers, response)["status"] == "queued"
    assert h.decisions.applied == []  # accepted is not applied
    h.applier.run()
    done = operation(h, headers, response)
    assert done["status"] == "applied" and len(h.decisions.applied) == 1
    # The package is closed once applied.
    assert h.client.get("/mobile/v1/reviews", headers=headers).json() == {"items": []}
    # The accepted payload is not kept after it is applied.
    assert h.store.operation(response.json()["operation_id"]).payload is None


def test_retry_after_lost_response_returns_the_same_operation(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    first = submit(h, headers)
    again = submit(h, headers)  # 202 lost, phone retries identically
    assert again.status_code == 202
    assert again.json()["operation_id"] == first.json()["operation_id"]
    h.applier.run()
    late = submit(h, headers, match='"stale-etag"')  # even after it was applied
    assert late.json()["operation_id"] == first.json()["operation_id"]
    assert late.json()["status"] == "applied"
    assert len(h.decisions.applied) == 1


def test_other_decision_for_the_same_revision_is_a_conflict(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    submit(h, headers)
    other = submit(h, headers, verdict(action="reject"))
    assert (other.status_code, other.json()["code"]) == (409, "verdict_conflict")
    h.applier.run()
    assert [v.action for v in h.decisions.applied] == ["approve"]


def test_decision_already_recorded_over_files_conflicts_or_dedups(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    with h.summary.connect() as db:  # a different verdict arrived via files
        db.execute(
            "INSERT INTO reviews VALUES (?,?,?,?,?)",
            ("pkg_a", 1, json.dumps(verdict(action="reject")), "{}", "PREPARED"),
        )
    conflict = submit(h, headers)
    assert (conflict.status_code, conflict.json()["code"]) == (409, "verdict_conflict")


def test_if_match_is_required_and_stale_revisions_are_refused(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    assert submit(h, headers, match=False).json()["code"] == "if_match_required"
    wrong = submit(h, headers, match='"nope"')
    assert (wrong.status_code, wrong.json()["code"]) == (412, "etag_mismatch")
    old_tag = etag(h, headers)
    h.add_package("pkg_a", 2)  # a newer revision supersedes while the phone was offline
    stale = h.client.put(
        f"{BASE}/1/verdict", json=verdict(), headers={**headers, "If-Match": old_tag}
    )
    assert (stale.status_code, stale.json()) == (
        412,
        {"code": "stale_revision", "retryable": False, "current_revision": 2},
    )
    assert h.store.pending("verdict") == []  # no auto-application to the new text
    ghost = submit(h, headers, verdict(revision=3), revision=3, match='"x"')
    assert ghost.status_code == 404


def test_verdict_validation(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    tag = {**headers, "If-Match": etag(h, headers)}
    bad = h.client.put(f"{BASE}/1/verdict", json={"action": "approve"}, headers=tag)
    assert bad.json()["code"] == "invalid_verdict"
    mismatch = h.client.put(f"{BASE}/1/verdict", json=verdict(revision=2), headers=tag)
    assert mismatch.json()["code"] == "id_mismatch"
    edit = h.client.put(f"{BASE}/1/verdict", json=verdict(action="edit"), headers=tag)
    assert edit.json()["code"] == "invalid_verdict"  # edit needs edited_body


def test_operations_are_private_to_their_device(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    response = submit(h, headers)
    other = h.pair(label="tablet")
    got = h.client.get(
        f"/mobile/v1/operations/{response.json()['operation_id']}", headers=other
    )
    assert got.status_code == 404
    assert (
        h.client.get("/mobile/v1/operations/op_missing", headers=headers).status_code
        == 404
    )


def test_applier_retries_transient_failures_then_stops_visibly(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    first = submit(h, headers)
    h.decisions.fail = RuntimeError("boom")
    h.tick()
    state = operation(h, headers, first)
    assert (state["status"], state["error"]) == ("applying", "apply_failed")
    h.tick(0.1)  # still in backoff: not retried yet
    assert operation(h, headers, first)["error"] == "apply_backoff"
    h.tick(10)
    h.tick(10)
    h.tick(10)
    stopped = operation(h, headers, first)
    assert (stopped["status"], stopped["error"]) == ("failed", "apply_stopped")
    h.decisions.fail = None  # code fixed; a restart retries stopped decisions
    h.tick(10)
    assert operation(h, headers, first)["status"] == "failed"
    h.applier.recover()
    h.tick(10)
    assert operation(h, headers, first)["status"] == "applied"
    assert len(h.decisions.applied) == 1


def test_applier_waits_while_the_llm_gate_is_closed(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    first = submit(h, headers)
    h.courier._can_apply = lambda: False
    h.tick()
    assert operation(h, headers, first)["error"] == "apply_waiting"
    h.courier._can_apply = None
    h.tick()
    assert operation(h, headers, first)["status"] == "applied"


def test_verdict_applied_over_files_is_not_applied_twice(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    first = submit(h, headers)
    # The same decision also arrives through the Syncthing folder first.
    (h.config.courier.decisions / "pkg_a.r1.json").write_text(json.dumps(verdict()))
    assert h.courier.scan_once().applied == [("pkg_a", 1)]
    h.applier.run()
    assert operation(h, headers, first)["status"] == "applied"
    assert len(h.decisions.applied) == 1


def test_crash_after_apply_before_journal_update_does_not_apply_again(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    first = submit(h, headers)
    h.applier.run()
    # Simulate a crash that lost the journal update (domain already applied).
    h.store.update_operation(first.json()["operation_id"], "queued")
    h.store._connect  # noqa: B018
    with h.store._connect() as db:
        db.execute(
            "UPDATE operations SET payload=? WHERE operation_id=?",
            (json.dumps(verdict()).replace(" ", ""), first.json()["operation_id"]),
        )
    h.applier.run()
    assert operation(h, headers, first)["status"] == "applied"
    assert len(h.decisions.applied) == 1


def test_stale_decision_after_new_revision_is_reported_stale(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    first = submit(h, headers)
    h.add_package("pkg_a", 2)  # a revision appears before the runtime applies it
    h.applier.run()
    assert operation(h, headers, first)["status"] == "stale"
    assert h.decisions.applied == []


def test_republish_writes_unfinished_packages_back_to_the_folder(tmp_path):
    h = Harness(tmp_path)
    h.add_package("pkg_a")
    h.add_package("pkg_b")
    proposals = Path(h.config.courier.proposals)
    written = h.reviews.republish(proposals)
    assert sorted(written) == ["pkg_a.r1.json", "pkg_b.r1.json"]
    assert h.reviews.republish(proposals) == []  # already identical
    (proposals / "pkg_a.r1.json").unlink()
    assert h.reviews.republish(proposals) == ["pkg_a.r1.json"]
