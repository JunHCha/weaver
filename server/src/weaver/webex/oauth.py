"""Integration OAuth (ADR-047), with no cached tokens or import-time I/O.

Protocol: https://developer.webex.com/admin/docs/authentication
Policy: docs/domain/architecture/webex-shepherd-pilot.md:22-26,54-76.
"""

import math
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .private import OAuthError, PrivateFile

AUTHORIZE_URL = "https://webexapis.com/v1/authorize"
TOKEN_URL = "https://webexapis.com/v1/access_token"
SCOPES = ("spark:rooms_read", "spark:messages_read", "spark:kms")
STATE_TTL_SECONDS = 180
# Treat tokens as expired this long before their stated expiry (clock skew,
# request latency); applies to both access and refresh tokens.
EXPIRY_MARGIN_SECONDS = 60


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    client_id: str = Field(min_length=1, repr=False)
    client_secret: str = Field(min_length=1, repr=False)
    redirect_uri: str = Field(repr=False)
    access_token: str | None = Field(default=None, repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    access_expires_at: float = 0
    refresh_expires_at: float = 0
    pending_state: str | None = Field(default=None, repr=False)
    pending_since: float = 0
    reauthentication_required: bool = True


def redirect_address(uri: str) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is None
            or not 1024 <= parsed.port <= 65535
            or not parsed.path.startswith("/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        return "127.0.0.1", parsed.port, parsed.path
    except ValueError:
        raise OAuthError("redirect_requires_registered_loopback_http_uri") from None


class OAuth:
    def __init__(
        self,
        path: Path,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.file = PrivateFile(path)
        self.client = client
        self.clock = clock

    def _read(self, parent: int, *, any_mode: bool = False) -> Credentials:
        data = self.file.read(parent, any_mode=any_mode)
        if data is None:
            raise OAuthError("oauth_not_configured")
        try:
            result = Credentials.model_validate_json(data)
            redirect_address(result.redirect_uri)
            return result
        except ValidationError:
            raise OAuthError("invalid_credentials") from None

    def _write(
        self, parent: int, credentials: Credentials, *, any_mode: bool = False
    ) -> None:
        self.file.write(
            parent, credentials.model_dump_json().encode(), any_mode=any_mode
        )

    def _access_fresh(self, credentials: Credentials) -> bool:
        return self.clock() < credentials.access_expires_at - EXPIRY_MARGIN_SECONDS

    def _refresh_usable(self, credentials: Credentials) -> bool:
        return bool(
            not credentials.reauthentication_required
            and credentials.refresh_token
            and self.clock() < credentials.refresh_expires_at - EXPIRY_MARGIN_SECONDS
        )

    def configure(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        redirect_address(redirect_uri)
        if not client_id.strip() or not client_secret.strip():
            raise OAuthError("client_credentials_required")
        with self.file.locked() as parent:
            if self.file.read(parent) is not None:
                raise OAuthError("already_configured")
            self._write(
                parent,
                Credentials(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri,
                ),
            )

    def redirect_uri(self) -> str:
        with self.file.locked() as parent:
            return self._read(parent).redirect_uri

    def status(self) -> str:
        with self.file.locked() as parent:
            value = self._read(parent)
            if not self._refresh_usable(value):
                return "reauthentication_required"
            return "authorized" if self._access_fresh(value) else "refresh_required"

    def begin(self) -> str:
        with self.file.locked() as parent:
            credentials = self._read(parent)
            credentials.pending_state = secrets.token_urlsafe(32)
            credentials.pending_since = self.clock()
            self._write(parent, credentials)
            return (
                AUTHORIZE_URL
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": credentials.client_id,
                        "redirect_uri": credentials.redirect_uri,
                        "scope": " ".join(SCOPES),
                        "state": credentials.pending_state,
                    }
                )
            )

    def cancel(self) -> None:
        with self.file.locked() as parent:
            credentials = self._read(parent)
            credentials.pending_state = None
            credentials.pending_since = 0
            self._write(parent, credentials)

    def complete(self, callback_uri: str) -> None:
        with self.file.locked() as parent:
            credentials = self._read(parent)
            try:
                parsed, expected = (
                    urlsplit(callback_uri),
                    urlsplit(credentials.redirect_uri),
                )
                if (parsed.scheme, parsed.netloc, parsed.path) != (
                    expected.scheme,
                    expected.netloc,
                    expected.path,
                ) or parsed.fragment:
                    raise ValueError
                query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=8)
                states = query.get("state", [])
                if (
                    len(states) != 1
                    or not credentials.pending_state
                    or not secrets.compare_digest(states[0], credentials.pending_state)
                ):
                    raise ValueError
                if (
                    not 0
                    <= self.clock() - credentials.pending_since
                    <= STATE_TTL_SECONDS
                ):
                    raise ValueError
            except (ValueError, UnicodeError, TypeError):
                raise OAuthError("invalid_oauth_callback") from None
            # Consume valid state before any exchange, including denial and HTTP failure.
            credentials.pending_state = None
            credentials.pending_since = 0
            self._write(parent, credentials)
            if "error" in query:
                raise OAuthError("consent_denied")
            codes = query.get("code", [])
            if len(codes) != 1 or not codes[0]:
                raise OAuthError("invalid_oauth_callback")
            self._exchange(
                parent,
                credentials,
                {
                    "grant_type": "authorization_code",
                    "code": codes[0],
                    "redirect_uri": credentials.redirect_uri,
                },
            )

    def _exchange(
        self, parent: int, credentials: Credentials, fields: dict[str, str]
    ) -> None:
        fields = {
            **fields,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
        }
        try:
            if self.client is None:
                with httpx.Client(
                    timeout=20, follow_redirects=False, trust_env=False
                ) as client:
                    response = client.post(TOKEN_URL, data=fields)
            else:
                response = self.client.post(
                    TOKEN_URL, data=fields, follow_redirects=False
                )
        except httpx.HTTPError:
            raise OAuthError("oauth_network_error") from None
        refreshing = fields["grant_type"] == "refresh_token"
        if response.status_code in (400, 401, 403):
            if not refreshing:
                # A failed code exchange must not destroy a still-valid grant.
                raise OAuthError("consent_failed")
            self._clear_tokens(credentials)
            self._write(parent, credentials)
            raise OAuthError("reauthentication_required")
        if response.status_code != 200:
            raise OAuthError("oauth_service_error")
        try:
            raw = response.json()
            if not isinstance(raw, dict):
                raise ValueError
            access = raw["access_token"]
            refresh = raw.get(
                "refresh_token", credentials.refresh_token if refreshing else None
            )
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(refresh, str)
                or not refresh
            ):
                raise ValueError
            refresh_lifetime = raw.get("refresh_token_expires_in")
            if refresh_lifetime is None and refreshing:
                # Conservative policy: without refresh_token_expires_in, inherit
                # the existing refresh expiry, even when a new refresh_token is sent.
                refresh_lifetime = credentials.refresh_expires_at - self.clock()
            lifetimes = [raw["expires_in"], refresh_lifetime]
            if any(
                type(item) not in (int, float) or not math.isfinite(item) or item <= 0
                for item in lifetimes
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise OAuthError("invalid_token_response") from None
        now = self.clock()
        credentials.access_token, credentials.refresh_token = access, refresh
        credentials.access_expires_at = now + lifetimes[0]
        credentials.refresh_expires_at = now + lifetimes[1]
        credentials.reauthentication_required = False
        try:
            self._write(parent, credentials)
        except (OAuthError, OSError):
            raise OAuthError("token_store_failed") from None

    @staticmethod
    def _clear_tokens(credentials: Credentials) -> None:
        """Drop tokens only; an in-progress consent's pending state is kept."""
        credentials.access_token = None
        credentials.refresh_token = None
        credentials.access_expires_at = 0
        credentials.refresh_expires_at = 0
        credentials.reauthentication_required = True

    def access_token(self, *, force_refresh: bool = False) -> str:
        """Poller entry: re-read durable state every time, refresh at most once.

        A caller may use force_refresh once after a resource request returns 401;
        it must stop and request consent if the retried resource still returns 401.
        """
        with self.file.locked() as parent:
            credentials = self._read(parent)
            refresh = credentials.refresh_token
            if not refresh or not self._refresh_usable(credentials):
                self._clear_tokens(credentials)
                self._write(parent, credentials)
                raise OAuthError("reauthentication_required")
            if (
                force_refresh
                or not credentials.access_token
                or not self._access_fresh(credentials)
            ):
                self._exchange(
                    parent,
                    credentials,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": refresh,
                    },
                )
            if credentials.access_token is None:
                raise OAuthError("reauthentication_required")
            return credentials.access_token

    def revoke(self) -> None:
        """Remove local tokens/state atomically; remote grant needs Authorized Apps."""
        with self.file.locked() as parent:
            # Tolerate wrong file mode (own regular file) so tokens can be removed;
            # the rewrite restores 0600. Links/owner/type checks still apply.
            credentials = self._read(parent, any_mode=True)
            self._clear_tokens(credentials)
            credentials.pending_state = None
            credentials.pending_since = 0
            self._write(parent, credentials, any_mode=True)
