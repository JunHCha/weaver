"""Synthetic peers and private tokens exercise listener isolation and rotation."""

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from weaver.app import cli
from weaver.config import ConfigError, load_config
from weaver.mobile.api import create_app
from weaver.mobile.store import MobileStore
from weaver.shepherd.registry import Registry

from .helpers import CAP, NODE, FakeWhois, write_config


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1"])
def test_local_capture_scope_host_origin_revoke_and_rotation(
    tmp_path: Path, bind: str
) -> None:
    config = write_config(tmp_path)
    store = MobileStore(config.state_root)
    registry = Registry(store)
    token_file = tmp_path / "secrets" / "courier.token"
    device = registry.register("mac-webex-primary", token_file)
    token = token_file.read_text().strip()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert token.encode() not in store.path.read_bytes()
    assert device.scopes == frozenset({"capture"})
    host = f"[{bind}]" if ":" in bind else bind
    app = create_app(config, None, store=store, local_bind=(bind, 8788))
    client = TestClient(app, base_url=f"http://{host}:8788", client=(bind, 40000))
    headers = {"Authorization": f"Bearer {token}"}
    receipt = f"/mobile/v1/captures/{CAP}/receipt"
    assert client.get(receipt, headers=headers).status_code == 404
    assert client.get("/mobile/v1/read/now", headers=headers).status_code == 403
    assert (
        client.get(receipt, headers={**headers, "Origin": "null"}).json()["code"]
        == "origin_not_allowed"
    )
    assert (
        client.get(receipt, headers={**headers, "Host": "localhost:8788"}).json()[
            "code"
        ]
        == "invalid_host"
    )
    assert registry.revoke("mac-webex-primary")
    assert client.get(receipt, headers=headers).json()["code"] == "token_revoked"
    replacement = registry.register("mac-webex-primary", token_file)
    assert replacement.device_id == device.device_id
    assert client.get(receipt, headers=headers).json()["code"] == "invalid_token"
    assert (
        client.get(
            receipt,
            headers={"Authorization": "Bearer " + token_file.read_text().strip()},
        ).status_code
        == 404
    )


def test_listener_and_real_peer_boundary_and_tailnet_whois(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    store = MobileStore(config.state_root)
    registry = Registry(store)
    token_file = tmp_path / "secrets" / "courier.token"
    registry.register("local", token_file)
    headers = {"Authorization": "Bearer " + token_file.read_text().strip()}
    app = create_app(config, None, store=store, local_bind=("127.0.0.1", 8788))
    client = TestClient(app, base_url="http://127.0.0.1:8788", client=("100.1.2.3", 2))
    assert (
        client.get(
            "/mobile/v1/capabilities",
            headers={**headers, "X-Forwarded-For": "127.0.0.1"},
        ).json()["code"]
        == "socket_mismatch"
    )
    client = TestClient(app, base_url="http://127.0.0.1:9999", client=("127.0.0.1", 2))
    assert (
        client.get(
            "/mobile/v1/capabilities", headers={**headers, "Host": "127.0.0.1:8788"}
        ).json()["code"]
        == "socket_mismatch"
    )
    whois = FakeWhois()
    tailnet = TestClient(
        create_app(config, whois, store=store),
        base_url="http://127.0.0.1:8787",
        client=("100.1.2.3", 2),
    )
    assert (
        tailnet.get("/mobile/v1/capabilities", headers=headers).json()["code"]
        == "socket_mismatch"
    )
    assert whois.calls == [("100.1.2.3", 2)]
    registry.register("remote", token_file, transport="tailnet", node_id=NODE)
    headers = {"Authorization": "Bearer " + token_file.read_text().strip()}
    assert tailnet.get("/mobile/v1/capabilities", headers=headers).status_code == 200
    local = TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 2))
    assert (
        local.get("/mobile/v1/capabilities", headers=headers).json()["code"]
        == "socket_mismatch"
    )
    whois.unavailable = True
    assert tailnet.get("/mobile/v1/capabilities", headers=headers).status_code == 503


def test_settings_and_cli_private_registration(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    config_file = tmp_path / "config.toml"
    token_file = tmp_path / "secrets" / "token"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "shepherd",
            "register",
            "one",
            "--token-file",
            str(token_file),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert token_file.read_text().strip() not in result.output
    assert (
        runner.invoke(
            cli, ["shepherd", "revoke", "one", "--config", str(config_file)]
        ).exit_code
        == 0
    )
    blocked = runner.invoke(
        cli,
        [
            "shepherd",
            "register",
            "one",
            "--token-file",
            str(config.sync_root / "secret"),
            "--config",
            str(config_file),
        ],
    )
    assert blocked.exit_code == 1
    assert "overlaps" in blocked.output
    text = config_file.read_text()
    config_file.write_text(
        text
        + '\n[shepherds.courier]\nmax_items=42\nmax_bytes=1024\nmin_free_bytes=1\nbind="::1"\nport=9898\n'
    )
    settings = load_config(config_file).shepherds.courier
    assert (settings.limits.max_items, settings.max_bytes, settings.min_free_bytes) == (
        42,
        1024,
        1,
    )
    for invalid in (
        'bind="0.0.0.0"',
        "max_items=0",
        "min_free_bytes=false",
        "port=8787",
        "unknown=1",
    ):
        config_file.write_text(text + "\n[shepherds.courier]\n" + invalid + "\n")
        with pytest.raises(ConfigError):
            load_config(config_file)


def test_loopback_upload_uses_existing_archive_journal_and_receipt(
    tmp_path: Path,
) -> None:
    import hashlib

    import httpx

    from weaver.schema import Manifest
    from weaver.shepherd.outbox import BundleKey, Outbox, OutboxLimits

    config = write_config(tmp_path)
    store = MobileStore(config.state_root)
    token_file = tmp_path / "secrets" / "token"
    Registry(store).register("one", token_file)
    local = TestClient(
        create_app(config, None, store=store, local_bind=("127.0.0.1", 8788)),
        base_url="http://127.0.0.1:8788",
        client=("127.0.0.1", 40000),
    )
    body = b"synthetic local capture"
    key = BundleKey("one", "space", "2026-11-20", 0, 0)
    manifest = Manifest.model_validate(
        {
            "schema_version": 1,
            "capture_id": key.capture_id,
            "capture_host": "mac",
            "kind": "text",
            "channel": "share_text",
            "payload": key.capture_id + ".txt",
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "captured_at": "2026-11-20T00:00:00Z",
            "occurred_at": None,
            "hints": {"counterpart": None},
        }
    )
    box = Outbox(
        config.state_root / "shepherds" / "one" / "outbox.sqlite3",
        "one",
        OutboxLimits(min_free_bytes=1),
    )
    box.buffer(key, body)
    box.close(key, manifest)
    headers = {"Authorization": "Bearer " + token_file.read_text().strip()}

    def respond(request: httpx.Request) -> httpx.Response:
        result = local.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={**dict(request.headers), **headers},
        )
        return httpx.Response(result.status_code, content=result.content)

    box.deliver(
        httpx.Client(
            base_url="http://127.0.0.1:8788/mobile/v1/",
            transport=httpx.MockTransport(respond),
        )
    )
    entry = box.get(key.capture_id)
    assert entry.receipt is not None and entry.payload is None
    assert (
        local.get(
            f"/mobile/v1/captures/{key.capture_id}/receipt", headers=headers
        ).content
        == entry.receipt
    )


def test_duplicate_authority_headers_are_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    app = create_app(config, None, local_bind=("127.0.0.1", 8788))
    client = TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 1))
    for headers in (
        [("Host", "127.0.0.1:8788"), ("Host", "evil.example")],
        [("Authorization", "Bearer one"), ("Authorization", "Bearer two")],
    ):
        result = client.get("/mobile/v1/capabilities", headers=headers)
        assert result.status_code == 400
        assert result.json()["code"] == "invalid_headers"


def test_registration_failure_keeps_previous_hash_and_rejects_unsafe_token_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weaver.mobile.store import hash_token
    from weaver.private import PrivateFile, PrivateFileError

    config = write_config(tmp_path)
    store = MobileStore(config.state_root)
    registry = Registry(store)
    token_file = tmp_path / "secrets" / "token"
    device = registry.register("one", token_file)
    token = token_file.read_text().strip()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("fake_private_failure")

    monkeypatch.setattr(PrivateFile, "write", fail)
    with pytest.raises(PrivateFileError):
        registry.register("one", token_file)
    assert store.device_for_token(token) == device
    with store._connect() as db:
        assert db.execute(
            "SELECT token_hash FROM devices WHERE device_id=?", (device.device_id,)
        ).fetchone()[0] == hash_token(token)
    token_file.chmod(0o644)
    # Validating a token for sending refuses permissive file modes, never repairs silently.
    with pytest.raises(PrivateFileError):
        private = PrivateFile(token_file)
        with private.locked() as parent:
            private.read(parent)
