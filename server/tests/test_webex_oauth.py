"""ADR-047 OAuth with fake credentials and MockTransport, never Webex calls.

Token response fixture follows the documented authentication response contract;
no assumption about real account permissions, retention, or message APIs.
"""

import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from weaver.app import cli
from weaver.webex.oauth import SCOPES, Credentials, OAuth, redirect_address
from weaver.webex.private import OAuthError

REDIRECT = "http://127.0.0.1:45123/oauth/callback"


def token_response(suffix: str = "one") -> dict[str, object]:
    return {
        "access_token": "fake-access-" + suffix,
        "refresh_token": "fake-refresh-" + suffix,
        "expires_in": 300,
        "refresh_token_expires_in": 3000,
    }


@pytest.fixture
def oauth(tmp_path: Path) -> OAuth:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://webexapis.com/v1/access_token"
        assert request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["client_secret"] == ["fake-secret"]
        return httpx.Response(200, json=token_response(form["grant_type"][0]))

    result = OAuth(
        tmp_path / "private" / "oauth.json",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: 1000.0,
    )
    result.configure("fake-client", "fake-secret", REDIRECT)
    return result


def callback(oauth: OAuth, **values: str) -> str:
    url = oauth.begin()
    query = parse_qs(urlsplit(url).query)
    assert set(query["scope"][0].split()) == set(SCOPES)
    assert query["redirect_uri"] == [REDIRECT]
    return (
        REDIRECT
        + "?"
        + urlencode({"state": query["state"][0], "code": "fake-code", **values})
    )


def authorize(oauth: OAuth) -> None:
    oauth.complete(callback(oauth))


def test_consent_state_and_private_permissions(oauth: OAuth) -> None:
    assert oauth.status() == "reauthentication_required"
    uri = callback(oauth)
    oauth.complete(uri)
    assert oauth.status() == "authorized"
    assert oauth.access_token() == "fake-access-authorization_code"
    assert stat.S_IMODE(oauth.file.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(oauth.file.path.parent.stat().st_mode) == 0o700
    assert "fake-" not in repr(
        Credentials.model_validate_json(oauth.file.path.read_bytes())
    )
    with pytest.raises(OAuthError, match="invalid_oauth_callback"):
        oauth.complete(uri)


@pytest.mark.parametrize(
    "alteration",
    [
        "wrong_state",
        "duplicate_state",
        "redirect",
        "fragment",
        "expired",
        "future",
        "nonascii",
    ],
)
def test_callback_rejections_without_http(oauth: OAuth, alteration: str) -> None:
    uri = callback(oauth)
    if alteration == "wrong_state":
        uri = REDIRECT + "?state=wrong&code=fake"
    elif alteration == "duplicate_state":
        uri += "&state=duplicate"
    elif alteration == "redirect":
        uri = uri.replace("45123", "45124")
    elif alteration == "fragment":
        uri += "#fragment"
    elif alteration == "expired":
        oauth.clock = lambda: 1181.0
    elif alteration == "future":
        oauth.clock = lambda: 999.0
    else:
        uri = REDIRECT + "?state=%ED%95%9C&code=fake"
    with pytest.raises(OAuthError, match="invalid_oauth_callback"):
        oauth.complete(uri)
    assert oauth.status() == "reauthentication_required"


def test_denial_and_new_attempt_invalidate_state(oauth: OAuth) -> None:
    first = callback(oauth)
    denied = callback(oauth, error="access_denied")
    with pytest.raises(OAuthError, match="invalid_oauth_callback"):
        oauth.complete(first)
    with pytest.raises(OAuthError, match="consent_denied"):
        oauth.complete(denied)
    with pytest.raises(OAuthError, match="invalid_oauth_callback"):
        oauth.complete(denied)
    assert "fake-access" not in oauth.file.path.read_text()


def test_rotation_uses_response_expiry_and_new_tokens(oauth: OAuth) -> None:
    authorize(oauth)
    oauth.clock = lambda: 1240.0
    assert oauth.status() == "refresh_required"
    assert oauth.access_token() == "fake-access-refresh_token"
    content = json.loads(oauth.file.path.read_bytes())
    assert content["refresh_token"] == "fake-refresh-refresh_token"
    assert content["access_expires_at"] == 1540
    assert content["refresh_expires_at"] == 4240
    assert "fake-access-authorization_code" not in oauth.file.path.read_text()


def test_concurrent_refresh_is_serialized(oauth: OAuth) -> None:
    authorize(oauth)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, json=token_response("rotated"))

    oauth.client = httpx.Client(transport=httpx.MockTransport(handler))
    oauth.clock = lambda: 1241.0
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: oauth.access_token(), range(2)))
    assert results == ["fake-access-rotated"] * 2
    assert len(calls) == 1


def test_revoke_clears_state_and_fresh_and_existing_clients_fail(oauth: OAuth) -> None:
    authorize(oauth)
    uri = callback(oauth)
    oauth.revoke()
    content = oauth.file.path.read_text()
    assert "fake-access" not in content and "fake-refresh" not in content
    assert json.loads(content)["pending_state"] is None
    assert json.loads(content)["client_secret"] == "fake-secret"
    for service in (oauth, OAuth(oauth.file.path)):
        assert service.status() == "reauthentication_required"
        with pytest.raises(OAuthError, match="reauthentication_required"):
            service.access_token()
        with pytest.raises(OAuthError, match="invalid_oauth_callback"):
            service.complete(uri)
    authorize(oauth)
    assert oauth.status() == "authorized"


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 302])
def test_http_errors_never_expose_provider_body(oauth: OAuth, status: int) -> None:
    authorize(oauth)
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="fake-private-provider-error")
        )
    )
    with pytest.raises(OAuthError) as error:
        oauth.access_token(force_refresh=True)
    assert "fake" not in str(error.value)
    if status in (400, 401, 403):
        assert oauth.status() == "reauthentication_required"
        assert "fake-access" not in oauth.file.path.read_text()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {**token_response(), "expires_in": True},
        {**token_response(), "refresh_token": ""},
        {**token_response(), "expires_in": -1},
        {**token_response(), "refresh_token_expires_in": "600"},
    ],
)
def test_invalid_response_does_not_partially_replace_tokens(
    oauth: OAuth, payload: object
) -> None:
    authorize(oauth)
    before = oauth.file.path.read_bytes()
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with pytest.raises(OAuthError, match="invalid_token_response"):
        oauth.access_token(force_refresh=True)
    assert oauth.file.path.read_bytes() == before


def test_timeout_and_expired_refresh(oauth: OAuth) -> None:
    authorize(oauth)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fake-sensitive", request=request)

    oauth.client = httpx.Client(transport=httpx.MockTransport(fail))
    with pytest.raises(OAuthError, match="oauth_network_error"):
        oauth.access_token(force_refresh=True)
    oauth.clock = lambda: 3940.0
    with pytest.raises(OAuthError, match="reauthentication_required"):
        oauth.access_token()
    assert "fake-access" not in oauth.file.path.read_text()


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/cb",
        "http://localhost:45123/cb",
        "http://0.0.0.0:45123/cb",
        "http://127.0.0.1/cb",
        "http://127.0.0.1:45123/cb?q=x",
        "http://user@127.0.0.1:45123/cb",
        "http://127.0.0.1:45123/cb#x",
    ],
)
def test_registered_loopback_redirect_only(uri: str) -> None:
    with pytest.raises(OAuthError, match="redirect"):
        redirect_address(uri)


@pytest.mark.parametrize("target", ["parent", "symlink", "hardlink", "lock"])
def test_unsafe_storage_is_rejected(oauth: OAuth, tmp_path: Path, target: str) -> None:
    if target == "parent":
        oauth.file.path.parent.chmod(0o755)
    elif target == "lock":
        oauth.file.path.with_suffix(".json.lock").chmod(0o644)
    elif target == "hardlink":
        os.link(oauth.file.path, tmp_path / "copy")
    else:
        original = oauth.file.path.read_bytes()
        oauth.file.path.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(original)
        outside.chmod(0o600)
        oauth.file.path.symlink_to(outside)
    with pytest.raises(OAuthError):
        oauth.revoke()


def test_no_overwrite_of_existing_registration(oauth: OAuth) -> None:
    before = oauth.file.path.read_bytes()
    with pytest.raises(OAuthError, match="already_configured"):
        oauth.configure("new-fake", "new-fake", REDIRECT)
    assert oauth.file.path.read_bytes() == before


def test_cli_status_revoke_and_help(
    oauth: OAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("weaver.webex.cli._oauth", lambda _: oauth)
    runner = CliRunner()
    authorize(oauth)
    result = runner.invoke(cli, ["webex", "status"])
    assert result.exit_code == 0 and result.stdout.strip() == "authorized"
    assert "lock" in result.stderr
    result = runner.invoke(cli, ["webex", "revoke"])
    assert result.exit_code == 0
    assert "lock" in result.stderr
    assert "Webex Authorized Apps" in result.output
    assert "fake-" not in result.output
    assert runner.invoke(cli, ["webex", "--help"]).exit_code == 0
    assert oauth.status() == "reauthentication_required"


def test_refresh_without_replacement_keeps_original_expiry(oauth: OAuth) -> None:
    authorize(oauth)
    oauth.clock = lambda: 1240.0
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"access_token": "fake-new", "expires_in": 25}
            )
        )
    )
    assert oauth.access_token() == "fake-new"
    content = json.loads(oauth.file.path.read_bytes())
    assert content["refresh_token"] == "fake-refresh-authorization_code"
    assert content["refresh_expires_at"] == 4000
    assert content["access_expires_at"] == 1265


@pytest.mark.parametrize(
    "behavior", ["success", "denied", "no_browser", "browser_failure", "timeout"]
)
def test_short_callback_listener_with_fake_http(
    oauth: OAuth, monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    import io
    from http.server import HTTPServer

    from weaver.webex.cli import receive_consent

    opened: list[str] = []
    calls: list[int] = []
    clock = [0.0]

    def browser(url: str) -> bool:
        opened.append(url)
        return behavior != "browser_failure"

    def init(self: HTTPServer, address: object, handler: object) -> None:
        assert address == ("127.0.0.1", 45123)
        self.RequestHandlerClass = handler

    def handle(self: HTTPServer) -> None:
        if behavior == "timeout":
            clock[0] = 181.0
            return
        pending = json.loads(oauth.file.path.read_bytes())["pending_state"]
        handler = self.RequestHandlerClass.__new__(self.RequestHandlerClass)
        handler.path = "/oauth/callback?" + urlencode(
            {
                "state": pending,
                "code": "fake-code",
                **({"error": "access_denied"} if behavior == "denied" else {}),
            }
        )
        handler.headers = {"Host": "127.0.0.1:45123"}
        handler.wfile = io.BytesIO()
        handler.send_response = calls.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.do_GET()

    monkeypatch.setattr(HTTPServer, "__init__", init)
    monkeypatch.setattr(HTTPServer, "handle_request", handle)
    monkeypatch.setattr(HTTPServer, "server_close", lambda _: None)
    monkeypatch.setattr("weaver.webex.cli.webbrowser.open", browser)
    monkeypatch.setattr("weaver.webex.cli.time.monotonic", lambda: clock[0])
    if behavior in ("denied", "browser_failure", "timeout"):
        expected = {
            "denied": "consent_denied",
            "browser_failure": "browser_unavailable",
            "timeout": "consent_timeout",
        }[behavior]
        with pytest.raises(OAuthError, match=expected):
            receive_consent(oauth)
    else:
        receive_consent(oauth, open_browser=behavior != "no_browser")
        assert oauth.status() == "authorized"
        assert calls == [200]
    assert json.loads(oauth.file.path.read_bytes())["pending_state"] is None


def test_atomic_write_failure_retains_previous_tokens_and_cleans_temp(
    oauth: OAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorize(oauth)
    before = oauth.file.path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("fake-private-failure")

    monkeypatch.setattr("weaver.webex.private.os.replace", fail)
    with pytest.raises(OAuthError, match="private_file_unavailable"):
        oauth.revoke()
    assert oauth.file.path.read_bytes() == before
    assert not list(oauth.file.path.parent.glob(".webex-*"))


def test_fifo_credentials_are_rejected_without_blocking(oauth: OAuth) -> None:
    oauth.file.path.unlink()
    os.mkfifo(oauth.file.path, 0o600)
    with pytest.raises(OAuthError, match="unsafe_private_permissions"):
        oauth.status()


def test_oversized_response_does_not_commit_unreadable_credentials(
    oauth: OAuth,
) -> None:
    authorize(oauth)
    before = oauth.file.path.read_bytes()
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={**token_response(), "access_token": "x" * 65536}
            )
        )
    )
    with pytest.raises(OAuthError, match="token_store_failed"):
        oauth.access_token(force_refresh=True)
    assert oauth.file.path.read_bytes() == before


def sent(oauth: OAuth) -> dict[str, object]:
    return json.loads(oauth.file.path.read_bytes())


def test_access_token_during_reauthentication_keeps_pending_consent(
    oauth: OAuth,
) -> None:
    uri = callback(oauth)  # begin() while in the reauthentication state
    with pytest.raises(OAuthError, match="reauthentication_required"):
        oauth.access_token()
    assert sent(oauth)["pending_state"] is not None
    oauth.complete(uri)
    assert oauth.status() == "authorized"


def test_refresh_failure_keeps_pending_consent(oauth: OAuth) -> None:
    authorize(oauth)
    uri = callback(oauth)
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(400))
    )
    with pytest.raises(OAuthError, match="reauthentication_required"):
        oauth.access_token(force_refresh=True)
    assert sent(oauth)["pending_state"] is not None
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=token_response("again"))
        )
    )
    oauth.complete(uri)
    assert oauth.access_token() == "fake-access-again"


def test_cancel_and_revoke_clear_pending_state(oauth: OAuth) -> None:
    callback(oauth)
    oauth.cancel()
    assert sent(oauth)["pending_state"] is None
    callback(oauth)
    oauth.revoke()
    assert sent(oauth)["pending_state"] is None


@pytest.mark.parametrize("status", [400, 401, 403])
def test_failed_code_exchange_keeps_existing_tokens(oauth: OAuth, status: int) -> None:
    authorize(oauth)
    before = sent(oauth)
    uri = callback(oauth)
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text="fake-x"))
    )
    with pytest.raises(OAuthError, match="consent_failed"):
        oauth.complete(uri)
    after = sent(oauth)
    for key in ("access_token", "refresh_token", "access_expires_at"):
        assert after[key] == before[key]
    assert after["pending_state"] is None
    assert oauth.status() == "authorized"


def test_expiry_margin_boundaries(oauth: OAuth) -> None:
    authorize(oauth)  # access expires 1300, refresh 4000; margin 60s
    for now, expected in [
        (1239.9, "authorized"),
        (1240.0, "refresh_required"),
        (3939.9, "refresh_required"),
        (3940.0, "reauthentication_required"),
    ]:
        oauth.clock = lambda now=now: now
        assert oauth.status() == expected
    oauth.clock = lambda: 1239.9
    assert oauth.access_token() == "fake-access-authorization_code"
    oauth.clock = lambda: 3939.9
    assert oauth.access_token() == "fake-access-refresh_token"
    oauth.clock = lambda: 3940.0 + 3000
    with pytest.raises(OAuthError, match="reauthentication_required"):
        oauth.access_token()


def test_refresh_rotation_without_lifetime_inherits_existing_expiry(
    oauth: OAuth,
) -> None:
    authorize(oauth)
    oauth.clock = lambda: 1240.0
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "access_token": "fake-new",
                    "refresh_token": "fake-rotated",
                    "expires_in": 300,
                },
            )
        )
    )
    assert oauth.access_token() == "fake-new"
    content = sent(oauth)
    assert content["refresh_token"] == "fake-rotated"
    assert content["refresh_expires_at"] == 4000  # inherited, never extended


def test_code_exchange_without_refresh_lifetime_is_invalid(oauth: OAuth) -> None:
    uri = callback(oauth)
    oauth.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "access_token": "fake-a",
                    "refresh_token": "fake-r",
                    "expires_in": 300,
                },
            )
        )
    )
    with pytest.raises(OAuthError, match="invalid_token_response"):
        oauth.complete(uri)


def test_save_failure_after_exchange_is_distinguished(
    oauth: OAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorize(oauth)
    original = oauth.file.write
    calls = []

    def fail(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise OSError("fake-private-failure")

    monkeypatch.setattr(oauth.file, "write", fail)
    with pytest.raises(OAuthError, match="token_store_failed"):
        oauth.access_token(force_refresh=True)
    assert calls and original


def test_revoke_repairs_wrong_file_mode(oauth: OAuth) -> None:
    authorize(oauth)
    oauth.file.path.chmod(0o644)
    oauth.revoke()
    assert stat.S_IMODE(oauth.file.path.stat().st_mode) == 0o600
    assert "fake-access" not in oauth.file.path.read_text()
    oauth.file.path.chmod(0o644)
    with pytest.raises(OAuthError, match="unsafe_private_permissions"):
        oauth.status()  # only revoke tolerates the mode


def test_revoke_cli_gives_manual_deletion_for_unsafe_storage(
    oauth: OAuth, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("weaver.webex.cli._oauth", lambda _: oauth)
    os.link(oauth.file.path, tmp_path / "copy")
    result = CliRunner().invoke(cli, ["webex", "revoke"])
    assert result.exit_code == 1
    assert "Delete the credentials file manually" in result.stderr
    assert "private/" in result.stderr
    assert "fake-" not in result.output


def test_symlinked_ancestor_is_rejected_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    service = OAuth(tmp_path / "link" / "private" / "oauth.json")
    with pytest.raises(OAuthError, match="unsafe_private_path"):
        service.redirect_uri()
    monkeypatch.setattr("weaver.webex.cli._oauth", lambda _: service)
    result = CliRunner().invoke(cli, ["webex", "status"])
    assert result.exit_code == 1 and "private/" in result.stderr


def test_configure_cli_never_echoes_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = OAuth(tmp_path / "private" / "oauth.json")
    monkeypatch.setattr("weaver.webex.cli._oauth", lambda _: service)
    runner = CliRunner()
    answers = "fake-client-id\nfake-client-secret\n" + REDIRECT + "\n"
    for _ in range(2):  # second run hits already_configured
        result = runner.invoke(cli, ["webex", "configure"], input=answers)
        assert "fake-client" not in result.output + result.stderr
    assert sent(service)["client_id"] == "fake-client-id"


def test_real_loopback_callback_rejects_bad_requests(tmp_path: Path) -> None:
    import http.client
    import socket
    import threading

    from weaver.webex.cli import receive_consent

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    redirect = f"http://127.0.0.1:{port}/oauth/callback"
    service = OAuth(
        tmp_path / "private" / "oauth.json",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=token_response())
            )
        ),
    )
    service.configure("fake-client", "fake-secret", redirect)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            receive_consent(service, open_browser=False)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not (state := sent(service)["pending_state"]) and time.time() < deadline:
        time.sleep(0.05)
    assert state
    good = f"/oauth/callback?state={state}&code=fake-code"

    def get(path: str, host: str | None = None) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{port}")
        connection.endheaders()
        try:
            return connection.getresponse().status
        finally:
            connection.close()

    assert get(good, host="evil.example") == 400
    assert get(good.replace("/oauth/callback", "/other")) == 400
    assert get(good + "&x=" + "a" * 9000) == 400
    assert sent(service)["pending_state"] == state  # nothing consumed
    assert get(good) == 200
    thread.join(10)
    assert not thread.is_alive() and not errors
    assert service.status() == "authorized"
