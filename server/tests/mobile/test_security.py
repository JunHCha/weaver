"""whois and token refusal, pairing, revocation: all with synthetic peers."""

import base64
import json
import os
import socket
import ssl
import stat
import threading
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from weaver.app import cli
from weaver.mobile import whois as whois_module
from weaver.mobile.store import DeviceError
from weaver.mobile.tls import ensure_certificates
from weaver.mobile.whois import (
    CachedWhois,
    CliWhois,
    LocalApiWhois,
    PeerIdentity,
    WhoisUnavailable,
    parse_whois,
)

from .helpers import LOGIN, NODE, Harness, write_config

CAPS = "/mobile/v1/capabilities"
WHOIS_JSON = json.dumps(
    {
        "Node": {"StableID": NODE, "Name": "fold.tail.ts.net."},
        "UserProfile": {"LoginName": LOGIN},
    }
).encode()


def test_unverifiable_peer_is_refused_before_the_token_is_considered(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.whois.identity = None  # tailscaled down or unknown peer: fail closed
    response = h.client.get(CAPS, headers=headers)
    assert (response.status_code, response.json()["code"]) == (403, "peer_unverified")
    assert h.whois.calls == [("100.108.10.0", 40000)]  # the real socket peer
    # Same refusal without any token: whois comes first.
    assert h.client.get(CAPS).json()["code"] == "peer_unverified"


def test_peer_must_be_the_node_the_token_is_bound_to(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.whois.identity = PeerIdentity("nOTHER", LOGIN)  # another own device
    response = h.client.get(CAPS, headers=headers)
    assert (response.status_code, response.json()["code"]) == (403, "node_mismatch")
    h.whois.identity = PeerIdentity(NODE, "intruder@example.com")  # shared-in node
    assert h.client.get(CAPS, headers=headers).json()["code"] == "node_mismatch"
    h.whois.identity = PeerIdentity(NODE, LOGIN)
    assert h.client.get(CAPS, headers=headers).status_code == 200


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer wvr_nope", "Basic abc", "bearer " + "x" * 80],
)
def test_missing_or_unknown_token_is_401(tmp_path, authorization):
    h = Harness(tmp_path)
    h.pair()
    headers = {} if authorization is None else {"Authorization": authorization}
    response = h.client.get(CAPS, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"code": "invalid_token", "retryable": False}
    assert response.headers["www-authenticate"] == "Bearer"


def test_token_of_another_device_does_not_work_from_this_node(tmp_path):
    h = Harness(tmp_path)
    tablet = h.pair(node="nTABLET", label="tablet")
    h.pair(label="fold")  # bound to NODE
    # The tablet's token is replayed from the phone's tailnet node.
    h.whois.identity = PeerIdentity(NODE, LOGIN)
    assert h.client.get(CAPS, headers=tablet).json()["code"] == "node_mismatch"


def test_host_origin_and_paths_are_locked_down(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    assert (
        h.client.get(CAPS, headers={**headers, "Host": "evil.example"}).json()["code"]
        == "invalid_host"
    )
    assert (
        h.client.get(CAPS, headers={**headers, "Host": "127.0.0.1:9999"}).status_code
        == 400
    )
    refused = h.client.get(
        CAPS, headers={**headers, "Origin": "https://127.0.0.1:8787"}
    )
    assert (refused.status_code, refused.json()["code"]) == (403, "origin_not_allowed")
    # The loopback web app's surface does not exist here.
    for path in ("/api/session", "/api/threads", "/", "/api/threads/x/tags"):
        assert h.client.get(path, headers=headers).status_code == 404
    assert h.client.post("/api/threads/x/tags", headers=headers).status_code == 404
    assert (
        "access-control-allow-origin" not in h.client.get(CAPS, headers=headers).headers
    )
    assert h.client.options(CAPS, headers=headers).status_code == 405


def test_scopes_separate_read_capture_review_action_and_edit(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    assert h.client.get("/mobile/v1/read/now", headers=headers).status_code == 200
    for method, path in (
        ("get", "/mobile/v1/reviews"),
        ("get", "/mobile/v1/captures/cap_01ARZ3NDEKTSV4RRFFQ69G5FA0/receipt"),
        ("post", "/mobile/v1/edits/tags"),
        ("get", "/mobile/v1/actions"),
    ):
        response = getattr(h.client, method)(path, headers=headers)
        assert response.status_code == 403, path
        assert response.json()["code"] in {"scope_denied"}


def test_revocation_closes_access_and_keeps_queued_work(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    package = h.client.get("/mobile/v1/reviews/pkg_a/revisions/1", headers=headers)
    from .helpers import verdict

    queued = h.client.put(
        "/mobile/v1/reviews/pkg_a/revisions/1/verdict",
        json=verdict(),
        headers={**headers, "If-Match": package.headers["etag"]},
    )
    assert queued.status_code == 202
    assert h.store.revoke(h.device.device_id)
    response = h.client.get(CAPS, headers=headers)
    assert (response.status_code, response.json()["code"]) == (401, "token_revoked")
    h.applier.run()  # the already-journaled decision is still applied
    assert h.decisions.applied
    assert not h.store.revoke(h.device.device_id)


def test_tokens_are_256_bit_random_and_stored_only_as_hashes(tmp_path):
    h = Harness(tmp_path)
    _, first = h.store.create_device("a", "n1", frozenset({"read"}))
    _, second = h.store.create_device("b", "n2", frozenset({"read"}))
    assert first != second and len(base64.urlsafe_b64decode(first[4:] + "==")) == 32
    database = (h.config.state_root / "mobile.sqlite3").read_bytes()
    wal = h.config.state_root / "mobile.sqlite3-wal"
    raw = database + (wal.read_bytes() if wal.exists() else b"")
    assert first.encode() not in raw and second.encode() not in raw
    mode = stat.S_IMODE((h.config.state_root / "mobile.sqlite3").stat().st_mode)
    assert mode == 0o600


def test_one_active_executor_and_validated_scopes(tmp_path):
    h = Harness(tmp_path)
    h.store.create_device("fold", "n1", frozenset({"read", "action"}), executor=True)
    with pytest.raises(DeviceError, match="executor_exists"):
        h.store.create_device("tab", "n2", frozenset({"action"}), executor=True)
    with pytest.raises(DeviceError, match="invalid_scope"):
        h.store.create_device("x", "n3", frozenset({"root"}))
    assert h.store.revoke(h.store.executor().device_id)
    assert h.store.create_device("tab", "n2", frozenset({"action"}), executor=True)


def test_logs_carry_only_opaque_ids_and_codes(tmp_path, caplog):
    h = Harness(tmp_path)
    headers = h.pair()
    h.whois.identity = PeerIdentity("nOTHER", LOGIN)
    with caplog.at_level("DEBUG"):
        h.client.get(CAPS, headers=headers)
        h.client.get(CAPS, headers={"Authorization": "Bearer wvr_secret"})
    text = caplog.text
    token = headers["Authorization"].split()[1]
    assert token not in text and "wvr_secret" not in text and LOGIN not in text


# ---- whois implementations against local fake tailscaled ------------------------


def test_parse_whois_is_strict():
    assert parse_whois(WHOIS_JSON) == PeerIdentity(NODE, LOGIN)
    for bad in (
        b"",
        b"{}",
        b'{"Node":{}}',
        b'{"Node":{"StableID":""}}',
        b"[1]",
        b'{"Node":null}',
    ):
        assert parse_whois(bad) is None


def serve_unix(
    path: Path, status: int, body: bytes, seen: list[bytes]
) -> threading.Thread:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def run() -> None:
        connection, _ = server.accept()
        seen.append(connection.recv(4096))
        reason = b"OK" if status == 200 else b"Forbidden"
        connection.sendall(
            b"HTTP/1.1 %d %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (status, reason, len(body), body)
        )
        connection.close()
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_localapi_whois_asks_about_the_exact_peer(tmp_path):
    path = Path("/tmp") / f"wv-{os.getpid()}-{os.urandom(3).hex()}.sock"
    seen: list[bytes] = []
    thread = serve_unix(path, 200, WHOIS_JSON, seen)
    try:
        identity = LocalApiWhois(path).lookup("100.108.10.0", 40000)
    finally:
        thread.join(2)
        path.unlink(missing_ok=True)
    assert identity == PeerIdentity(NODE, LOGIN)
    request = seen[0].decode()
    assert request.startswith("GET /localapi/v0/whois?addr=100.108.10.0%3A40000 ")
    assert "Host: local-tailscaled.sock" in request


def test_localapi_whois_distinguishes_unknown_peer_from_unavailable(tmp_path):
    with pytest.raises(WhoisUnavailable):  # tailscaled not running
        LocalApiWhois(tmp_path / "missing.sock").lookup("100.1.1.1", 1)
    path = Path("/tmp") / f"wv-{os.getpid()}-{os.urandom(3).hex()}.sock"
    thread = serve_unix(path, 404, b"no", [])
    try:
        assert LocalApiWhois(path).lookup("100.1.1.1", 1) is None  # answered: unknown
    finally:
        thread.join(2)
        path.unlink(missing_ok=True)
    thread = serve_unix(path, 403, b"no", [])
    try:
        with pytest.raises(WhoisUnavailable):  # any other status is not an answer
            LocalApiWhois(path).lookup("100.1.1.1", 1)
    finally:
        thread.join(2)
        path.unlink(missing_ok=True)


def script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_cli_whois_uses_the_binary_and_separates_unknown_from_unavailable(tmp_path):
    ok = script(
        tmp_path,
        "tailscale",
        f"[ \"$1\" = whois ] || exit 9\nprintf '%s' '{WHOIS_JSON.decode()}'\n",
    )
    assert CliWhois(ok).lookup("100.108.10.0", 5) == PeerIdentity(NODE, LOGIN)
    unknown = script(tmp_path, "unknown", "echo 'no match for IP:port' >&2\nexit 1\n")
    assert CliWhois(unknown).lookup("100.1.1.1", 5) is None
    for body in ("exit 1\n", "printf junk\n", "sleep 0\nexit 3\n"):
        with pytest.raises(WhoisUnavailable):
            CliWhois(script(tmp_path, "broken", body)).lookup("100.1.1.1", 5)
    with pytest.raises(WhoisUnavailable):
        CliWhois(tmp_path / "absent").lookup("100.1.1.1", 5)


def test_cli_whois_defaults_to_the_app_bundle_binary_when_present(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "Tailscale"
    bundle.write_text("")
    monkeypatch.setattr(whois_module, "APP_BUNDLE_CLI", bundle)
    assert CliWhois()._executable == str(bundle)
    monkeypatch.setattr(whois_module, "APP_BUNDLE_CLI", tmp_path / "none")
    assert CliWhois()._executable == "tailscale"


def test_whois_answers_are_cached_per_peer_for_five_seconds(tmp_path):
    class Counting:
        def __init__(self):
            self.calls = []
            self.answer = PeerIdentity(NODE, LOGIN)

        def lookup(self, host, port):
            self.calls.append((host, port))
            if self.answer is None:
                raise WhoisUnavailable
            return self.answer

    inner, now = Counting(), [100.0]
    cached = CachedWhois(inner, 5.0, lambda: now[0])
    assert cached.lookup("100.1.1.1", 1) == cached.lookup("100.1.1.1", 1)
    assert len(inner.calls) == 1
    cached.lookup("100.1.1.1", 2)  # a different connection is a different key
    assert len(inner.calls) == 2
    now[0] += 5.1
    cached.lookup("100.1.1.1", 1)
    assert len(inner.calls) == 3
    inner.answer = None  # failures are never remembered
    now[0] += 6
    for _ in range(2):
        with pytest.raises(WhoisUnavailable):
            cached.lookup("100.1.1.1", 1)
    assert len(inner.calls) == 5


def test_transient_whois_failure_is_503_and_unknown_peer_is_403(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.whois.unavailable = True
    down = h.client.get(CAPS, headers=headers)
    assert (down.status_code, down.json()) == (
        503,
        {"code": "whois_unavailable", "retryable": True},
    )
    assert down.headers["retry-after"] == "2"
    h.whois.unavailable = False
    h.whois.identity = None
    assert h.client.get(CAPS, headers=headers).status_code == 403
    h.whois.identity = PeerIdentity(NODE, LOGIN)
    assert h.client.get(CAPS, headers=headers).status_code == 200


# ---- pairing CLI ---------------------------------------------------------------


def test_pair_devices_and_revoke_commands(tmp_path, monkeypatch):
    write_config(tmp_path)
    config = str(tmp_path / "config.toml")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "mobile",
            "pair",
            "--config",
            config,
            "--label",
            "fold",
            "--node-id",
            NODE,
            "--executor",
            "--no-qr",
            "--print-json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["server_url"] == "https://127.0.0.1:8787" and payload[
        "token"
    ].startswith("wvr_")
    assert (
        payload["ca_sha256"]
        == ensure_certificates(
            tmp_path / "state" / "mobile" / "tls", "127.0.0.1"
        ).ca_sha256
    )
    listing = runner.invoke(cli, ["mobile", "devices", "--config", config])
    assert (
        "fold" in listing.output
        and "executor" in listing.output
        and payload["token"] not in listing.output
    )
    twice = runner.invoke(
        cli,
        [
            "mobile",
            "pair",
            "--config",
            config,
            "--label",
            "x",
            "--node-id",
            "n2",
            "--executor",
            "--no-qr",
            "--print-json",
        ],
    )
    assert twice.exit_code == 1 and "executor_exists" in twice.output
    device_id = payload["device_id"]
    assert (
        runner.invoke(
            cli, ["mobile", "revoke", device_id, "--config", config]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli, ["mobile", "revoke", device_id, "--config", config]
        ).exit_code
        == 1
    )
    no_choice = runner.invoke(
        cli, ["mobile", "pair", "--config", config, "--label", "y"]
    )
    assert no_choice.exit_code != 0


def test_pair_can_prefill_the_node_through_whois(tmp_path):
    write_config(tmp_path)
    script = tmp_path / "tailscale"
    script.write_text(f"#!/bin/sh\nprintf '%s' '{WHOIS_JSON.decode()}'\n")
    script.chmod(0o755)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        config_path.read_text() + f'whois = "cli"\ntailscale_cli = "{script}"\n'
    )
    result = CliRunner().invoke(
        cli,
        [
            "mobile",
            "pair",
            "--config",
            str(config_path),
            "--label",
            "fold",
            "--phone-ip",
            "100.108.10.0",
            "--no-qr",
            "--print-json",
        ],
    )
    assert result.exit_code == 0, result.output
    from weaver.mobile.store import MobileStore

    (device,) = MobileStore(tmp_path / "state").devices()
    assert (device.node_id, device.login_name) == (NODE, LOGIN)


def test_serve_requires_a_configured_bind(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            f'{n}_root = "{n}"' for n in ("sync", "archive", "vault", "state", "log")
        )
    )
    from weaver.config import load_config

    load_config(path, init_directories=True)
    result = CliRunner().invoke(cli, ["mobile", "serve", "--config", str(path)])
    assert result.exit_code == 78


# ---- HTTPS ----------------------------------------------------------------------


def test_certificates_are_private_renewable_and_keep_the_ca(tmp_path):
    first = ensure_certificates(tmp_path / "tls", "127.0.0.1")
    for name in ("ca.key", "server.key"):
        assert stat.S_IMODE((tmp_path / "tls" / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "tls").stat().st_mode) == 0o700
    original = first.server_cert.read_bytes()
    again = ensure_certificates(tmp_path / "tls", "127.0.0.1")
    assert again.server_cert.read_bytes() == original
    before = first.ca_sha256
    moved = ensure_certificates(tmp_path / "tls", "100.64.1.2")  # new tailnet IP
    assert moved.ca_sha256 == before  # paired phones need no new trust step
    assert moved.server_cert.read_bytes() != original


def test_https_listener_only_speaks_tls_and_requires_the_paired_ca(tmp_path):
    import time

    import uvicorn

    h = Harness(tmp_path)
    headers = h.pair()
    tls = ensure_certificates(tmp_path / "tls", "127.0.0.1")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(
        uvicorn.Config(
            h.app,
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(tls.server_cert),
            ssl_keyfile=str(tls.server_key),
            log_level="critical",
            access_log=False,
            proxy_headers=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        assert server.started
        # Host must match the configured bind:port, so pin the header for the test port.
        host = {"Host": "127.0.0.1:8787"}
        trusted = httpx.get(
            f"https://127.0.0.1:{port}{CAPS}",
            headers={**headers, **host},
            verify=ssl.create_default_context(cafile=str(tls.ca_cert)),
        )
        assert trusted.status_code == 200
        # A client that does not trust the Weaver CA cannot connect: no insecure fallback.
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"https://127.0.0.1:{port}{CAPS}", headers=headers)
        # Plain HTTP gets no API response at all.
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{port}{CAPS}", headers=headers)
    finally:
        server.should_exit = True
        thread.join(5)
