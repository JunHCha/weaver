"""Alert HTTP envelope/receipt contract; dismiss lives only in the phone DB."""

import json
from datetime import timedelta

import pytest
from server.tests.alert.test_delivery import alert

from weaver.alert import AlertHistory
from weaver.alert.history import AlertBodyExpired
from weaver.config import ConfigError, MobileSettings
from weaver.mobile.api import create_app
from weaver.mobile.transport import MobileRuntime, reconcile_transports
from weaver.runtime.assemble import build_runtime

from .helpers import Harness

HTTP = '[mobile.features]\nalert = "http"\n'
BASE = "/mobile/v1/alerts"


def seed(h):
    history = AlertHistory(h.config.state_root / "alerts.sqlite3")
    body = alert()
    history.stage(body, "thr_1")
    history.projected(body.alert_id)
    return history, body


@pytest.mark.parametrize("settings", ["", HTTP])
def test_invalid_file_does_not_block_startup_or_reconciliation(
    tmp_path, settings, caplog
):
    h = Harness(tmp_path, settings)
    path = h.config.courier.proposals / "alert_invalid.json"
    path.write_text("invalid JSON")
    build_runtime(h.config, environ={})  # construct only; never invoke an LLM
    create_app(h.config, h.whois)
    create_app(h.config, None, local_bind=("127.0.0.1", 8788))
    assert (
        reconcile_transports(h.config.mobile, h.store, h.reviews, h.actions, h.config)
        == []
    )
    assert path.read_text() == "invalid JSON"
    assert "alert migration skipped: alert_invalid.json" in caplog.text


def test_receipt_is_device_specific_durable_and_not_dismiss(tmp_path):
    h = Harness(tmp_path, HTTP)
    one, two = h.pair("review"), h.pair("review")
    history, body = seed(h)
    listed = h.client.get(BASE, headers=one)
    assert listed.status_code == 200
    assert listed.json() == {
        "items": [
            body.model_dump(mode="json") | {"created_at": "2026-11-07T03:00:00Z"}
        ],
        "next_cursor": None,
    }
    url = f"{BASE}/{body.alert_id}"
    fetched = h.client.get(url, headers=one)
    assert fetched.json() == listed.json()["items"][0]
    assert fetched.headers["etag"]
    for _ in range(2):
        ack = h.client.put(url + "/ack", headers=one)
        assert ack.status_code == 200
        assert ack.json() == {"alert_id": body.alert_id, "state": "received"}
    assert h.client.get(BASE, headers=one).json()["items"] == []
    assert len(h.client.get(BASE, headers=two).json()["items"]) == 1
    assert history.body(body.alert_id) == body
    assert h.client.get(url, headers=one).status_code == 200
    assert AlertHistory(history.path).bodies() == [body]
    # Revocation/re-pairing creates a new device identity, never inherits an ack.
    h.store.revoke(h.device.device_id)
    assert h.client.get(BASE, headers=two).status_code == 401
    repaired = h.pair("review")
    assert len(h.client.get(BASE, headers=repaired).json()["items"]) == 1
    # Another server's DB never inherits a receipt with the same alert ID.
    other = Harness(tmp_path / "other", HTTP)
    other_headers = other.pair("review")
    seed(other)
    assert other.store.server_id != h.store.server_id
    assert len(other.client.get(BASE, headers=other_headers).json()["items"]) == 1


def test_scope_route_and_unknown_alert(tmp_path):
    h = Harness(tmp_path, HTTP)
    for scopes in (("capture",), ("read",), ("action",)):
        headers = h.pair(*scopes)
        for method, suffix in (
            ("get", ""),
            ("get", "/unknown"),
            ("put", "/unknown/ack"),
        ):
            assert (
                getattr(h.client, method)(BASE + suffix, headers=headers).status_code
                == 403
            )
    headers = h.pair("review")
    assert h.client.get(BASE + "/unknown", headers=headers).status_code == 404
    assert h.client.put(BASE + "/unknown/ack", headers=headers).status_code == 404
    files = Harness(tmp_path / "files")
    headers = files.pair("review")
    assert files.client.get(BASE, headers=headers).json()["code"] == "transport_files"


def test_alert_transport_requires_explicit_apk_rollout_opt_in(tmp_path):
    h = Harness(tmp_path, 'transport = "http"\n')
    assert h.config.mobile.transport_for("alert") == "files"
    headers = h.pair("review")
    assert not h.client.get("/mobile/v1/capabilities", headers=headers).json()[
        "features"
    ]["alerts"]
    enabled = Harness(tmp_path / "enabled", HTTP)
    headers = enabled.pair("review")
    assert enabled.config.mobile.transport_for("review") == "files"
    caps = enabled.client.get("/mobile/v1/capabilities", headers=headers).json()
    assert caps["transports"]["alert"] == "http"
    assert caps["schema_versions"]["alert"] == 2
    assert enabled.client.get("/mobile/v1/capabilities", headers=headers).json()[
        "features"
    ]["alerts"]


def test_transport_reconciliation_replays_acks_only_once(tmp_path):
    h = Harness(tmp_path, HTTP)
    history, body = seed(h)
    history.ack("phone", body.alert_id, body.created_at)

    def reconcile(h):
        return reconcile_transports(
            h.config.mobile, h.store, h.reviews, h.actions, h.config
        )

    assert reconcile(h) == []
    back = Harness(tmp_path)
    assert reconcile(back) == ["alert"]
    path = back.config.courier.proposals / f"alert_{body.alert_id}.json"
    assert json.loads(path.read_text()) == body.model_dump(mode="json")
    path.unlink()
    assert reconcile(back) == []
    assert not path.exists()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "30"])
def test_retention_validation(value):
    with pytest.raises(ConfigError, match="mobile.alert_body_days"):
        MobileSettings(alert_body_days=value)


def test_retention_toml(tmp_path):
    assert MobileSettings().alert_body_days == 30
    h = Harness(tmp_path, "alert_body_days = 45\n")
    assert h.config.mobile.alert_body_days == 45


def test_expired_body_and_ack_are_gone_for_existing_and_new_devices(tmp_path):
    h = Harness(tmp_path, HTTP)
    headers = h.pair("review")
    device = h.device.device_id
    history, body = seed(h)
    history.ack(device, body.alert_id, body.created_at)
    assert history.collect(30, [device], body.created_at + timedelta(days=31)) == 1
    new_headers = h.pair("review")
    for who in (headers, new_headers):
        assert h.client.get(BASE, headers=who).json()["items"] == []
        for method, suffix in (("get", ""), ("put", "/ack")):
            response = getattr(h.client, method)(
                f"{BASE}/{body.alert_id}{suffix}", headers=who
            )
            assert response.status_code == 410
            assert response.json()["code"] == "alert_body_expired"
            unknown = getattr(h.client, method)(f"{BASE}/unknown{suffix}", headers=who)
            assert unknown.status_code == 404
            assert unknown.json()["code"] == "alert_not_found"
    with history._connect() as db:
        assert db.execute("SELECT count(*) FROM alert_ack").fetchone()[0] == 1


def test_maintenance_collects_only_http_bodies_received_by_active_reviewers(tmp_path):
    for route in ("http", "files"):
        h = Harness(tmp_path / route, f'[mobile.features]\nalert = "{route}"\n')
        history, body = seed(h)
        h.pair("review")
        recipient = h.device.device_id
        h.pair("read")  # read-only devices are not Alert recipients
        h.pair("review")
        revoked = h.device.device_id
        h.store.revoke(revoked)
        now = body.created_at + timedelta(days=100)
        runtime = MobileRuntime(
            h.config,
            h.store,
            h.applier,
            h.reviews,
            h.actions,
            clock=lambda now=now: now,
        )
        runtime.run()
        assert history.body(body.alert_id) == body  # no receipt, however old
        history.ack(recipient, body.alert_id, now - timedelta(days=31))
        runtime.run()
        if route == "http":
            with pytest.raises(AlertBodyExpired):
                history.body(body.alert_id)
        else:
            assert history.body(body.alert_id) == body
        assert history.issued(body.alert_id)
