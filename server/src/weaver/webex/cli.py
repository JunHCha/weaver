"""Typer settings/consent commands; no secrets in arguments, output, or logs."""

from __future__ import annotations

import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import socket
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

import typer

from .private import OAuthError

if TYPE_CHECKING:
    from .oauth import OAuth

webex_cli = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
ConfigOption = Annotated[str | None, typer.Option("--config", help="Server TOML path")]


def _oauth(config: str | None) -> OAuth:
    from weaver.app import _load

    from .oauth import OAuth

    path = _load(config).shepherds.webex.credentials_file
    if path is None:
        typer.echo("Error: shepherds.webex.credentials_file is required", err=True)
        raise typer.Exit(78)
    return OAuth(path)


def _error(error: OAuthError) -> None:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(1) from None


@webex_cli.command()
def configure(config: ConfigOption = None) -> None:
    """Register the Integration's client credentials in its private file.

    Register an exact http://127.0.0.1:PORT/PATH redirect with Webex first.
    Existing credentials are never overwritten; revoke clears tokens, retaining
    client registration for a new consent flow.
    """
    oauth = _oauth(config)
    client_id = typer.prompt("Integration client ID", hide_input=True)
    secret = typer.prompt("Integration client secret", hide_input=True)
    redirect = typer.prompt("Registered loopback redirect URI")
    try:
        oauth.configure(client_id, secret, redirect)
    except OAuthError as error:
        _error(error)
    typer.echo(
        "Integration configured; run webex consent. New Spaces are included unless excluded by ID."
    )


def receive_consent(oauth: OAuth, *, open_browser: bool = True) -> None:
    from .oauth import STATE_TTL_SECONDS, redirect_address

    uri = oauth.redirect_uri()
    host, port, path = redirect_address(uri)
    complete = False
    failure: OAuthError | None = None

    class LocalServer(HTTPServer):
        def get_request(self) -> tuple[socket, tuple[str, int]]:
            connection, address = super().get_request()
            connection.settimeout(5)
            return connection, address

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # Callback query contains credentials: never log it.

        def do_GET(self) -> None:
            nonlocal complete, failure
            result = "Authorization refused. Return to the terminal."
            status = 400
            try:
                valid_path = urlsplit(self.path).path == path
            except ValueError:
                valid_path = False
            if (
                self.headers.get("Host") == urlsplit(uri).netloc
                and valid_path
                and len(self.path) <= 8192
            ):
                try:
                    oauth.complete(uri + self.path[len(path) :])
                    complete = True
                    status, result = (
                        200,
                        "Authorization complete. You may close this window.",
                    )
                except OAuthError as error:
                    if str(error) != "invalid_oauth_callback":
                        failure = error
            body = result.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        # Bind before opening the browser, never wildcard/public or persistent.
        with LocalServer((host, port), Callback) as server:
            server.timeout = 1
            url = oauth.begin()
            try:
                if open_browser:
                    if not webbrowser.open(url):
                        raise OAuthError("browser_unavailable")
                else:
                    typer.echo(url)
                deadline = time.monotonic() + STATE_TTL_SECONDS
                while not complete and failure is None and time.monotonic() < deadline:
                    server.handle_request()
                if failure is not None:
                    raise failure
                if not complete:
                    raise OAuthError("consent_timeout")
            finally:
                oauth.cancel()
    except OSError:
        raise OAuthError("callback_listener_unavailable") from None


@webex_cli.command()
def consent(
    config: ConfigOption = None,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print consent URL for manual opening")
    ] = False,
) -> None:
    """Request minimum read scopes using a short-lived local callback."""
    try:
        receive_consent(_oauth(config), open_browser=not no_browser)
    except OAuthError as error:
        _error(error)
    typer.echo("Webex authorized.")


@webex_cli.command()
def status(config: ConfigOption = None) -> None:
    """Show authentication state without reading Spaces or printing tokens."""
    try:
        typer.echo(_oauth(config).status())
    except OAuthError as error:
        _error(error)


@webex_cli.command()
def revoke(config: ConfigOption = None) -> None:
    """Remove local access/refresh tokens and pending consent; keep queued work."""
    try:
        _oauth(config).revoke()
    except OAuthError as error:
        _error(error)
    typer.echo(
        "Local tokens and pending consent removed; reauthentication required. To revoke the remote grant, open Webex Authorized Apps and remove this Integration. Courier credentials and queued work are retained."
    )
