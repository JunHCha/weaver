"""Synthetic crash upload -> private durable file -> operator CLI."""

import json
import logging
import stat
from concurrent.futures import ThreadPoolExecutor

from typer.testing import CliRunner

from weaver.app import cli
from weaver.mobile.crashes import CrashStore

from .helpers import Harness

REPORT = {
    "client_id": "crash_synthetic_0001",
    "occurred_at": "2026-11-19T00:00:00Z",
    "app_version": "0.1.0-debug",
    "app_build": 1,
    "thread": "main",
    "exceptions": [
        {
            "exception_class": "java.lang.IllegalStateException",
            "frames": [
                {
                    "class_name": "io.github.junhcha.weaver.ui.Map",
                    "method_name": "open",
                    "file_name": "Map.kt",
                    "line_number": 42,
                }
            ],
        }
    ],
    "screen": {
        "last_tab": "NOW",
        "map_open": True,
        "selected_node": "USER",
        "settings_open": False,
    },
}
PATH = "/mobile/v1/diagnostics/crashes"


def test_crash_authenticated_scope_free_durable_and_cli(tmp_path, caplog):
    h = Harness(tmp_path)
    # Read-only pairing can submit diagnostics without any write scope.
    _, token = h.store.create_device("synthetic", "nPHONE0001", frozenset({"read"}))
    headers = {"Authorization": f"Bearer {token}"}
    with caplog.at_level(logging.INFO, logger="weaver.mobile"):
        response = h.client.post(PATH, json=REPORT, headers=headers)
    assert response.status_code == 201
    assert response.json() == {"client_id": REPORT["client_id"], "state": "stored"}
    assert response.headers["cache-control"] == "no-store"
    path = h.config.state_root / "mobile/crashes/crash_synthetic_0001.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["report"]["exceptions"] == REPORT["exceptions"]
    assert [r.message for r in caplog.records if r.name == "weaver.mobile"] == [
        "crash crash_synthetic_0001"
    ]
    # Re-open the store, as after a process restart; retry does not create a second file.
    assert (
        CrashStore(h.config.state_root).submit(
            json.dumps(REPORT).encode(), "repaired-device"
        )[0]
        == 200
    )
    assert len(list(path.parent.glob("*.json"))) == 1
    args = ["mobile", "crashes", "--config", str(tmp_path / "config.toml")]
    listed = CliRunner().invoke(cli, args)
    assert listed.exit_code == 0 and REPORT["client_id"] in listed.output
    detail = CliRunner().invoke(cli, [*args, REPORT["client_id"]])
    assert detail.exit_code == 0
    assert json.loads(detail.output)["report"]["exceptions"] == REPORT["exceptions"]
    missing = CliRunner().invoke(cli, [*args, "../private"])
    assert missing.exit_code == 1 and "crash_not_found" in missing.output


def test_crash_accepts_log_tab(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    log = {**REPORT, "screen": {**REPORT["screen"], "last_tab": "LOG"}}
    assert h.client.post(PATH, json=log, headers=headers).status_code == 201
    # Older APKs still send the retired tab names.
    for n, old in enumerate(("PEOPLE", "WIKI")):
        report = {
            **log,
            "client_id": f"crash_synthetic_old{n}",
            "screen": {**REPORT["screen"], "last_tab": old},
        }
        assert h.client.post(PATH, json=report, headers=headers).status_code == 201
    unknown = {
        **REPORT,
        "client_id": "crash_synthetic_map",
        "screen": {**REPORT["screen"], "last_tab": "MAP"},
    }
    assert h.client.post(PATH, json=unknown, headers=headers).status_code == 422


def test_crash_refuses_messages_conflicts_size_and_unauthenticated(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    assert h.client.post(PATH, json=REPORT).status_code == 401
    for malicious in (
        {**REPORT, "message": "PRIVATE SYNTHETIC MESSAGE"},
        {
            **REPORT,
            "exceptions": [
                {**REPORT["exceptions"][0], "message": "PRIVATE SYNTHETIC MESSAGE"}
            ],
        },
        {**REPORT, "client_id": "../private"},
        {**REPORT, "occurred_at": "2026-11-19T00:00:00"},
    ):
        response = h.client.post(PATH, json=malicious, headers=headers)
        assert response.status_code == 422
        assert "PRIVATE" not in response.text
    assert not list((h.config.state_root / "mobile/crashes").glob("*.json"))
    assert h.client.post(PATH, json=REPORT, headers=headers).status_code == 201
    assert (
        h.client.post(
            PATH, json={**REPORT, "app_build": 2}, headers=headers
        ).status_code
        == 409
    )
    assert (
        h.client.post(PATH, content=b"x" * (65536 + 1), headers=headers).status_code
        == 413
    )
    # No declared length (chunked): streaming limit still applies.
    assert (
        h.client.post(
            PATH, content=iter([b"x" * 32768] * 3), headers=headers
        ).status_code
        == 413
    )
    assert "PRIVATE" not in "".join(
        p.read_text() for p in (h.config.state_root / "mobile/crashes").glob("*.json")
    )


def test_crash_parallel_replay_publishes_one_complete_file(tmp_path):
    store = CrashStore(tmp_path)
    raw = json.dumps(REPORT).encode()
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: store.submit(raw, "device")[0], range(8)))
    assert sorted(statuses) == [200] * 7 + [201]
    assert len(store.records()) == 1
    assert not list(store.root.glob(".crash-*"))
