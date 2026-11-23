"""Decided contract details: operation shape, Z times, read model additions,
snapshot reuse, owner takeover and idempotent retries after 5xx."""

import re

from fastapi import HTTPException
from typer.testing import CliRunner

from weaver.app import cli
from weaver.fabric import ThreadQuery
from weaver.mobile import edits as edits_module
from weaver.mobile import read as read_module

from .helpers import Harness, result, schedule, thread, verdict
from .test_edits import corrections
from .test_edits import seed as seed_edits
from .test_read import at
from .test_read import seed as seed_read

Z = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
OPERATION = {
    "operation_id",
    "state",
    "kind",
    "target_id",
    "target_revision",
    "updated_at",
}


def datetimes(value, found=None):
    found = [] if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if key != "frontmatter":
                datetimes(item, found)
    elif isinstance(value, list):
        for item in value:
            datetimes(item, found)
    elif isinstance(value, str) and re.match(r"\d{4}-\d\d-\d\dT\d\d:", value):
        found.append(value)
    return found


def test_operation_shape_and_states(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    etag = h.client.get(
        "/mobile/v1/reviews/pkg_a/revisions/1", headers=headers
    ).headers["etag"]
    queued = h.client.put(
        "/mobile/v1/reviews/pkg_a/revisions/1/verdict",
        json=verdict(),
        headers={**headers, "If-Match": etag},
    ).json()
    assert set(queued) == OPERATION and queued["state"] == "queued"
    assert (queued["kind"], queued["target_id"], queued["target_revision"]) == (
        "verdict",
        "pkg_a",
        1,
    )
    assert Z.match(queued["updated_at"])
    h.decisions.fail = RuntimeError("x")
    h.tick()
    waiting = h.client.get(
        f"/mobile/v1/operations/{queued['operation_id']}", headers=headers
    ).json()
    assert (waiting["state"], waiting["code"]) == ("queued", "apply_failed")
    h.decisions.fail = None
    h.tick(10)
    done = h.client.get(
        f"/mobile/v1/operations/{queued['operation_id']}", headers=headers
    ).json()
    assert done["state"] == "applied" and "code" not in done


def test_refused_and_stopped_operations_have_distinct_states(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    etag = h.client.get(
        "/mobile/v1/reviews/pkg_a/revisions/1", headers=headers
    ).headers["etag"]
    first = h.client.put(
        "/mobile/v1/reviews/pkg_a/revisions/1/verdict",
        json=verdict(),
        headers={**headers, "If-Match": etag},
    ).json()
    h.add_package("pkg_a", 2)
    h.applier.run()  # a newer revision arrived before the decision was applied
    refused = h.client.get(
        f"/mobile/v1/operations/{first['operation_id']}", headers=headers
    ).json()
    assert (refused["state"], refused["code"]) == ("rejected", "stale_rev")
    h.store.update_operation(first["operation_id"], "failed", error="apply_stopped")
    stopped = h.client.get(
        f"/mobile/v1/operations/{first['operation_id']}", headers=headers
    ).json()
    assert (stopped["state"], stopped["code"]) == ("failed", "apply_stopped")


def test_every_datetime_in_responses_is_utc_with_z(tmp_path):
    h = Harness(tmp_path)
    seed_read(h)
    h.add_package()
    headers = h.pair("read", "review", "capture")
    for path in (
        "/mobile/v1/read/threads",
        "/mobile/v1/read/now",
        "/mobile/v1/read/timeline?subject=네트워크&history=true",
        "/mobile/v1/read/states",
        "/mobile/v1/reviews/pkg_a/revisions/1",
    ):
        body = h.client.get(path, headers=headers).json()
        found = datetimes(body)
        assert found, path
        assert all(Z.match(value) for value in found), (path, found)
    # The instant is preserved: 09:00 Seoul is 00:00 UTC.
    first = h.client.get("/mobile/v1/read/threads/t1", headers=headers).json()
    assert first["occurred_at"] == "2026-11-05T00:00:00Z"


def test_thread_source_label_and_mentions_come_from_the_server(tmp_path):
    h = Harness(tmp_path)
    seed_read(h)
    headers = h.pair("read")
    by_id = {
        t["thread_id"]: t
        for t in h.client.get("/mobile/v1/read/threads", headers=headers).json()[
            "items"
        ]
    }
    # t3 tags 서준(alias of 이서준) but its counterpart is 한지우: 이서준 is a mention.
    assert by_id["t3"]["mentions"] == ["이서준"]
    assert by_id["t1"]["mentions"] == ["협력사 A"]  # the counterpart is not a mention
    assert by_id["t2"]["mentions"] == []
    assert by_id["t1"]["source"] == "알 수 없음"  # no archived source in this dataset
    assert isinstance(by_id["t4"]["effective_schedule"], dict)
    assert {"presentation", "open", "retracted", "effective_deadline"} <= set(
        by_id["t1"]
    )


def test_source_labels_follow_the_archived_channel(tmp_path):
    from .helpers import manifest, put_manifest, upload

    h = Harness(tmp_path)
    headers = h.pair("read", "capture")
    upload(h, headers)
    receipt = put_manifest(
        h,
        headers,
        manifest(
            channel="call",
            occurred_at="2026-11-07T12:00:00+09:00",
            hints={"counterpart": "이서준"},
        ),
    ).json()
    h.fabric.append([thread("c1", source_id=receipt["source_id"]), thread("c2")])
    items = {
        t["thread_id"]: t["source"]
        for t in h.client.get("/mobile/v1/read/threads", headers=headers).json()[
            "items"
        ]
    }
    assert items == {"c1": "이서준", "c2": "알 수 없음"}


def test_dictionary_read_endpoint(tmp_path):
    h = Harness(tmp_path)
    seed_edits(h)
    headers = h.pair("read")
    page = h.client.get("/mobile/v1/read/dictionary", headers=headers).json()
    assert page["next_cursor"] is None
    by_name = {e["name"]: e for e in page["items"]}
    assert set(by_name["Alpha"]) == {
        "name",
        "kind",
        "aliases",
        "usage_count",
        "archived",
    }
    assert by_name["Alpha"]["aliases"] == ["A"] and by_name["Alpha"]["usage_count"] == 2
    assert by_name["Beta"]["kind"] == "person"


def test_capabilities_features_flag_alerts_and_pipeline_as_files_only(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    features = h.client.get("/mobile/v1/capabilities", headers=headers).json()[
        "features"
    ]
    assert features["alerts"] is False and features["pipeline"] is False
    assert features["capture"] and features["review"] and features["action"]
    assert features["read"] and features["dictionary"] and features["edits"]


def test_thread_pages_reuse_one_snapshot_until_the_data_changes(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    seed_read(h)
    headers = h.pair("read")
    calls = []
    real = read_module.canonical_threads
    monkeypatch.setattr(
        read_module,
        "canonical_threads",
        lambda *a, **k: calls.append(1) or real(*a, **k),
    )
    cursor, pages = None, 0
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = h.client.get(
            "/mobile/v1/read/threads", params=params, headers=headers
        ).json()
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 5 and len(calls) == 1  # not one full recompute per page
    h.client.get("/mobile/v1/read/now", headers=headers)
    h.client.get("/mobile/v1/read/timeline?subject=네트워크", headers=headers)
    assert len(calls) == 1
    h.fabric.append(
        [thread("later", tags=["네트워크"], occurred_at=at("13"), recorded_at=at("13"))]
    )
    last = h.client.get(
        "/mobile/v1/read/threads", params={"cursor": "c10"}, headers=headers
    ).json()
    assert [t["thread_id"] for t in last["items"]] == ["later"] and len(calls) == 2


def test_now_reads_the_snapshot_once(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    seed_read(h)
    calls = []
    real = read_module.canonical_threads
    monkeypatch.setattr(
        read_module,
        "canonical_threads",
        lambda *a, **k: calls.append(1) or real(*a, **k),
    )
    read_module.ReadModel(h.config).now(at("12", 8))
    assert len(calls) == 1


def test_resend_takes_over_the_operation_for_the_current_device(tmp_path):
    h = Harness(tmp_path)
    old = h.pair(label="old phone")
    h.add_package()
    etag = h.client.get("/mobile/v1/reviews/pkg_a/revisions/1", headers=old).headers[
        "etag"
    ]
    url = "/mobile/v1/reviews/pkg_a/revisions/1/verdict"
    first = h.client.put(url, json=verdict(), headers={**old, "If-Match": etag}).json()
    old_device = h.device.device_id
    assert h.store.revoke(old_device)
    new = h.pair(label="new phone")  # re-paired phone, same node
    assert h.device.device_id != old_device
    assert (
        h.client.get(
            f"/mobile/v1/operations/{first['operation_id']}", headers=new
        ).status_code
        == 404
    )
    again = h.client.put(url, json=verdict(), headers={**new, "If-Match": etag})
    assert again.json()["operation_id"] == first["operation_id"]
    assert (
        h.client.get(
            f"/mobile/v1/operations/{first['operation_id']}", headers=new
        ).status_code
        == 200
    )
    # Same for client-ID edits (replayed result).
    seed_edits(h)
    body = {"client_id": "client-owner-01", "thread_ids": ["record"], "add": ["Beta"]}
    edit = h.client.post(
        "/mobile/v1/edits/tags", json=body, headers={**new, "X": "1"}
    ).json()
    assert h.store.operation(edit["operation_id"]).device_id == h.device.device_id
    third = h.pair(label="third")
    h.client.post("/mobile/v1/edits/tags", json=body, headers=third)
    assert h.store.operation(edit["operation_id"]).device_id == h.device.device_id


def test_pair_prints_a_qr_only_unless_print_json_is_given(tmp_path):
    from .helpers import write_config

    write_config(tmp_path)
    config = str(tmp_path / "config.toml")
    runner = CliRunner()
    quiet = runner.invoke(
        cli, ["mobile", "pair", "--config", config, "--label", "a", "--node-id", "n1"]
    )
    assert quiet.exit_code == 0, quiet.output
    assert (
        "wvr_" not in quiet.output and "{" not in quiet.output
    )  # no plaintext token or payload
    assert (
        "█" in quiet.output or "▀" in quiet.output or "▄" in quiet.output
    )  # the QR is there
    loud = runner.invoke(
        cli,
        [
            "mobile",
            "pair",
            "--config",
            config,
            "--label",
            "b",
            "--node-id",
            "n2",
            "--print-json",
        ],
    )
    assert "wvr_" in loud.output
    refused = runner.invoke(
        cli,
        [
            "mobile",
            "pair",
            "--config",
            config,
            "--label",
            "c",
            "--node-id",
            "n3",
            "--no-qr",
        ],
    )
    assert refused.exit_code != 0
    from weaver.mobile.store import MobileStore

    assert [d.label for d in MobileStore(tmp_path / "state").devices()] == [
        "a",
        "b",
    ]  # c issued nothing


def test_single_action_lookup_does_not_scan_every_lineage(tmp_path):
    from weaver.action import ActionJob

    h = Harness(tmp_path, 'transport = "http"\n')
    job = ActionJob(h.fabric, h.outbox, h.actions)
    h.fabric.append([schedule("s1"), schedule("s2", tags=("다른 사람",))])
    job.run()
    fold = h.pair("read", "action", executor=True)
    first = h.outbox.all()[0]

    def forbidden():
        raise AssertionError("scanned every lineage")

    h.actions.dispatchable = forbidden  # type: ignore[method-assign]
    one = h.client.get(f"/mobile/v1/actions/{first.action_id}", headers=fold)
    assert one.status_code == 200 and one.json()["action_id"] == first.action_id
    assert (
        h.client.get("/mobile/v1/actions/act_" + "0" * 32, headers=fold).status_code
        == 404
    )


def test_a_later_action_in_a_lineage_is_not_offered_individually(tmp_path):
    from weaver.action import ActionJob

    h = Harness(tmp_path, 'transport = "http"\n')
    job = ActionJob(h.fabric, h.outbox, h.actions)
    h.fabric.append([schedule("s1")])
    job.run()
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
    fold = h.pair("read", "action", executor=True)
    register, update = h.outbox.all()
    assert (
        h.client.get(
            f"/mobile/v1/actions/{register.action_id}", headers=fold
        ).status_code
        == 200
    )
    assert (
        h.client.get(f"/mobile/v1/actions/{update.action_id}", headers=fold).status_code
        == 404
    )


# ---- retries after 5xx never duplicate Fabric rows ----------------------------


def test_tag_edit_retry_after_a_503_following_the_commit_never_duplicates(
    tmp_path, monkeypatch
):
    h = Harness(tmp_path)
    seed_edits(h)
    headers = h.pair("edit")
    real = edits_module.apply_tags
    attempts = []

    def commit_then_fail(*args, **kwargs):
        done = real(*args, **kwargs)  # Fabric rows are written...
        if not attempts:
            attempts.append(1)
            raise HTTPException(503, "busy")  # ...but the answer is lost
        return done

    monkeypatch.setattr(edits_module, "apply_tags", commit_then_fail)
    body = {
        "client_id": "client-503-0001",
        "thread_ids": ["record", "second"],
        "add": ["Beta"],
    }
    first = h.client.post("/mobile/v1/edits/tags", json=body, headers=headers)
    assert (first.status_code, first.json()) == (
        503,
        {"code": "dictionary_busy", "retryable": True},
    )
    assert len(corrections(h)) == 2  # committed once already
    for _ in range(3):  # the phone keeps retrying the same request
        retry = h.client.post("/mobile/v1/edits/tags", json=body, headers=headers)
        assert retry.status_code == 200 and retry.json()["state"] == "applied"
    assert len(corrections(h)) == 2
    ids = [c.thread_id for c in corrections(h)]
    assert sorted(ids) == sorted(retry.json()["result"]["correction_ids"])
    assert (
        len(h.fabric.entries(ThreadQuery(include_executions=True))) == 5
    )  # 3 seed + 2


def test_tag_edit_retry_after_an_unhandled_502_style_failure_never_duplicates(
    tmp_path, monkeypatch
):
    h = Harness(tmp_path)
    seed_edits(h)
    headers = h.pair("edit")
    real_update = h.store.update_operation
    failed = []

    def lose_the_journal_once(*args, **kwargs):
        if not failed and kwargs.get("result"):
            failed.append(1)
            raise RuntimeError("process died after the Fabric commit")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(h.store, "update_operation", lose_the_journal_once)
    body = {"client_id": "client-502-0001", "thread_ids": ["record"], "add": ["Beta"]}
    from fastapi.testclient import TestClient

    quiet = TestClient(
        h.app,
        base_url="https://127.0.0.1:8787",
        client=("100.108.10.0", 40000),
        raise_server_exceptions=False,
    )
    boom = quiet.post("/mobile/v1/edits/tags", json=body, headers=headers)
    assert boom.status_code == 500 and boom.json()["retryable"] is True
    assert len(corrections(h)) == 1
    retry = h.client.post("/mobile/v1/edits/tags", json=body, headers=headers)
    assert retry.status_code == 200 and retry.json()["state"] == "applied"
    assert len(corrections(h)) == 1


def test_dictionary_edit_retry_after_a_busy_503_applies_once(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    seed_edits(h)
    headers = h.pair("edit")
    real = edits_module.add_alias
    calls = []

    def busy_once(config, edit):
        if not calls:
            calls.append(1)
            raise HTTPException(503, "daemon holds the lock")
        return real(config, edit)

    monkeypatch.setattr(edits_module, "add_alias", busy_once)
    body = {"client_id": "client-alias-001", "entry": "Alpha", "alias": "AA"}
    busy = h.client.post(
        "/mobile/v1/edits/dictionary/alias-add", json=body, headers=headers
    )
    assert (busy.status_code, busy.json()["retryable"]) == (503, True)
    ok = h.client.post(
        "/mobile/v1/edits/dictionary/alias-add", json=body, headers=headers
    )
    again = h.client.post(
        "/mobile/v1/edits/dictionary/alias-add", json=body, headers=headers
    )
    assert ok.status_code == again.status_code == 200 and again.json() == ok.json()
    assert again.headers["idempotent-replay"] == "true"
    entry = [
        e
        for e in h.client.get(
            "/mobile/v1/read/dictionary", headers=h.pair("read", label="r")
        ).json()["items"]
        if e["name"] == "Alpha"
    ][0]
    assert entry["aliases"].count("AA") == 1


def test_retry_after_the_503_response_of_a_result_put_is_the_same_operation(tmp_path):
    from weaver.action import ActionJob

    h = Harness(tmp_path, 'transport = "http"\n')
    job = ActionJob(h.fabric, h.outbox, h.actions)
    h.fabric.append([schedule("s1")])
    job.run()
    fold = h.pair("read", "action", executor=True)
    (record,) = h.outbox.all()
    url = f"/mobile/v1/actions/{record.action_id}/result"
    ids = {
        h.client.put(url, json=result(record.action_id), headers=fold).json()[
            "operation_id"
        ]
        for _ in range(3)
    }
    h.applier.run()
    ids |= {
        h.client.put(url, json=result(record.action_id), headers=fold).json()[
            "operation_id"
        ]
    }
    assert len(ids) == 1
    assert len(h.fabric.entries(ThreadQuery(kind="실행"))) == 1


def test_results_and_feedback_flags_need_a_vault_tree(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    features = h.client.get("/mobile/v1/capabilities", headers=headers).json()[
        "features"
    ]
    assert features["results"] is False and features["feedback"] is False
