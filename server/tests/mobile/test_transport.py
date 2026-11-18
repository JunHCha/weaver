"""Per-feature transport switching, return to files, and runtime wiring."""

import json
import logging
import threading

from typer.testing import CliRunner

from weaver.action import ActionJob
from weaver.app import cli
from weaver.config import MobileSettings
from weaver.mobile.transport import reconcile_transports
from weaver.runtime.assemble import build_runtime

from .helpers import (
    BODY,
    Harness,
    result,
    schedule,
    verdict,
    write_config,
)

HTTP = 'transport = "http"\n'


def reconcile(h):
    return reconcile_transports(
        h.config.mobile, h.store, h.reviews, h.actions, h.config
    )


def test_capabilities_report_the_transport_of_each_feature(tmp_path):
    h = Harness(tmp_path, '[mobile.features]\ncapture = "http"\naction = "http"\n')
    headers = h.pair("read")
    body = h.client.get("/mobile/v1/capabilities", headers=headers).json()
    assert body["transports"] == {
        "capture": "http",
        "review": "files",
        "action": "http",
    }
    assert body["limits"]["max_payload_bytes"] == 64 * 1024 * 1024
    assert body["server_id"].startswith("srv_") and body["feed_epoch"].startswith("ep_")


def test_settings_validation():
    import pytest

    from weaver.config import ConfigError

    assert MobileSettings().bind is None  # disabled until configured
    for bad in ("0.0.0.0", "::", "192.168.0.5", "10.0.0.1", "example.com", "8.8.8.8"):
        with pytest.raises(ConfigError):
            MobileSettings(bind=bad)
    for good in ("100.106.199.83", "fd7a:115c:a1e0::1", "127.0.0.1", "::1"):
        assert MobileSettings(bind=good).bind == good
    for kwargs in (
        {"port": 0},
        {"transport": "ftp"},
        {"max_payload_bytes": 0},
        {"staging_days": 0},
    ):
        with pytest.raises(ConfigError):
            MobileSettings(**kwargs)


def test_config_file_parsing_and_rejection(tmp_path):
    import pytest

    from weaver.config import ConfigError, load_config

    h = Harness(tmp_path / "ok", '[mobile.features]\nreview = "http"\n')
    assert h.config.mobile.transport_for("review") == "http"
    assert h.config.mobile.transport_for("capture") == "files"
    assert h.config.mobile.whois == "cli"
    for extra in (
        "bogus = 1\n",
        'whois = "magic"\n',
        'bind = "0.0.0.0"\n',
        '[mobile.features]\nvault = "http"\n',
        '[mobile.features]\ncapture = "ftp"\n',
    ):
        path = tmp_path / "bad"
        path.mkdir(exist_ok=True)
        text = write_config(tmp_path / "base").config_file.read_text()
        text = (
            text.replace('bind = "127.0.0.1"\n', "")
            if extra.startswith("bind")
            else text
        )
        (path / "config.toml").write_text(text + extra)
        with pytest.raises(ConfigError):
            load_config(path / "config.toml", init_directories=True)


def test_switch_to_files_republishes_unfinished_items_and_blocks_http_dispatch(
    tmp_path,
):
    h = Harness(tmp_path, HTTP)
    job = ActionJob(h.fabric, h.outbox, h.actions)
    h.fabric.append([schedule("s1")])
    job.run()
    h.add_package("pkg_a")
    assert reconcile(h) == []  # first run only records the transports
    assert h.store.recorded_transport("action") == "http"
    assert list((h.config.sync_root / "actions").glob("*.json")) == []
    fold = h.pair("read", "action", "review", executor=True)
    (offered,) = h.client.get("/mobile/v1/actions", headers=fold).json()["items"]
    # A result and a verdict are accepted over HTTP right before the switch...
    h.client.put(
        f"/mobile/v1/actions/{offered['action_id']}/result",
        json=result(offered["action_id"]),
        headers=fold,
    )
    # ...while a second action stays unfinished.
    h.fabric.append([schedule("s2", tags=("다른 사람",))])
    job.run()

    back = Harness(tmp_path, "")  # restart with the transport set back to files
    assert sorted(reconcile(back)) == ["action", "capture", "review"]
    assert {p.name for p in (back.config.courier.proposals).glob("pkg_a.r1.json")} == {
        "pkg_a.r1.json"
    }
    back_job = ActionJob(back.fabric, back.outbox, back.actions)
    back_job.run()
    pending = [r for r in back.outbox.all() if r.status == "pending"]
    published = {p.stem for p in (back.config.sync_root / "actions").glob("*.json")}
    assert published and published <= {r.action_id for r in back.outbox.all()}
    assert offered["action_id"] in {r.action_id for r in back.outbox.all()}
    assert all(
        r.action_id in published for r in pending if r.kind == "calendar.register"
    )
    assert reconcile(back) == []  # nothing changes on the next pass
    # HTTP no longer dispatches, but late writes of earlier work are still absorbed.
    # (the paired token lives in the shared state, so it works after the restart)
    assert (
        back.client.get("/mobile/v1/actions", headers=fold).json()["code"]
        == "transport_files"
    )
    late = back.client.put(
        f"/mobile/v1/actions/{offered['action_id']}/result",
        json=result(offered["action_id"]),
        headers=fold,
    )
    assert late.status_code == 202
    back.applier.run()
    assert back.store.operation(late.json()["operation_id"]).status == "applied"

    # And switching to http again withdraws the files so one executor owns dispatch.
    again = Harness(tmp_path, HTTP)
    assert reconcile(again) == []
    again.actions.publish()
    assert list((again.config.sync_root / "actions").glob("*.json")) == []


def test_pending_http_verdicts_survive_a_switch_and_are_applied_once(tmp_path):
    h = Harness(tmp_path, HTTP)
    reconcile(h)
    headers = h.pair("review")
    h.add_package()
    etag = h.client.get(
        "/mobile/v1/reviews/pkg_a/revisions/1", headers=headers
    ).headers["etag"]
    queued = h.client.put(
        "/mobile/v1/reviews/pkg_a/revisions/1/verdict",
        json=verdict(),
        headers={**headers, "If-Match": etag},
    )
    assert queued.status_code == 202
    back = Harness(tmp_path, "")
    reconcile(back)
    # The same decision now also arrives through the folder (phone back on files).
    (back.config.courier.decisions / "pkg_a.r1.json").write_text(json.dumps(verdict()))
    back.courier.scan_once()
    back.applier.run()
    assert back.store.operation(queued.json()["operation_id"]).status == "applied"
    assert len(back.decisions.applied) == 1


def test_status_and_reconcile_commands(tmp_path):
    h = Harness(tmp_path, HTTP)
    config = str(h.config.config_file)
    runner = CliRunner()
    first = runner.invoke(cli, ["mobile", "reconcile", "--config", config])
    assert first.exit_code == 0 and "nothing" in first.output
    text = runner.invoke(cli, ["mobile", "status", "--config", config]).output
    assert (
        "capture: configured=http recorded=http" in text
        and "pending verdict: 0" in text
    )
    write_config(tmp_path, "")  # back to files
    h.add_package("pkg_z")
    out = runner.invoke(cli, ["mobile", "reconcile", "--config", config]).output
    assert "capture" in out and "review" in out and "action" in out
    assert (h.config.courier.proposals / "pkg_z.r1.json").is_file()


def test_resident_runtime_applies_phone_operations_through_the_real_journals(
    tmp_path, caplog
):
    """No LLM: capture archives and the verdict waits, visibly, for the gate."""
    h = Harness(tmp_path, HTTP)
    config = write_config(tmp_path, HTTP)
    runtime = build_runtime(config, environ={})
    assert "mobile" in [task.name for task in runtime.maintenance]
    headers = h.pair("capture", "review", "read")
    from .helpers import put_manifest, upload

    assert upload(h, headers).status_code == 201
    assert put_manifest(h, headers).status_code == 201
    h.add_package("pkg_r")
    etag = h.client.get(
        "/mobile/v1/reviews/pkg_r/revisions/1", headers=headers
    ).headers["etag"]
    queued = h.client.put(
        "/mobile/v1/reviews/pkg_r/revisions/1/verdict",
        json=verdict("pkg_r"),
        headers={**headers, "If-Match": etag},
    )
    stop = threading.Event()
    waits = iter([False, True])
    runtime._wait = lambda _timeout: next(waits)
    with caplog.at_level(logging.DEBUG):
        runtime.run(stop)
    status = json.loads((config.state_root / "runtime-status.json").read_text())
    assert sum(status["sources"].values()) == 1  # the HTTP capture entered the pipeline
    state = h.client.get(
        f"/mobile/v1/operations/{queued.json()['operation_id']}", headers=headers
    ).json()
    assert state["state"] == "queued" and state["code"] == "apply_waiting"
    assert (
        BODY.decode() not in caplog.text and headers["Authorization"] not in caplog.text
    )


def test_runtime_without_a_bind_has_no_mobile_task(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    path = root / "config.toml"
    path.write_text(
        "\n".join(
            f'{n}_root = "{n}"' for n in ("sync", "archive", "vault", "state", "log")
        )
    )
    from weaver.config import load_config

    runtime = build_runtime(load_config(path, init_directories=True), environ={})
    assert "mobile" not in [task.name for task in runtime.maintenance]
    assert not (root / "state" / "mobile.sqlite3").exists()
