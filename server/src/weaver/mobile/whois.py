"""Identify the real socket peer through tailscaled (LocalAPI or ``tailscale whois``).

Only the TCP peer address of the connection is asked about; hostnames and
headers are never trusted. Every failure (daemon down, timeout, unknown peer,
malformed answer) yields ``None`` so callers fail closed.
"""

import http.client
import json
import logging
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

logger = logging.getLogger("weaver.mobile")
TIMEOUT_SECONDS = 3.0
CACHE_SECONDS = 5.0
# The App Store build ships its CLI inside the app bundle (no tailscaled socket).
APP_BUNDLE_CLI = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


class WhoisUnavailable(Exception):
    """tailscaled could not answer (down, timeout, bad output); retry later."""


@dataclass(frozen=True)
class PeerIdentity:
    node_id: str
    login_name: str | None


class Whois(Protocol):
    def lookup(self, host: str, port: int) -> PeerIdentity | None:
        """The peer's identity, ``None`` if tailscale does not know the address.

        Raises ``WhoisUnavailable`` when the answer could not be obtained.
        """
        ...


def parse_whois(raw: bytes) -> PeerIdentity | None:
    """Strict parse; malformed or incomplete answers yield ``None``."""
    try:
        data = json.loads(raw)
        node = data["Node"]
        node_id = node["StableID"]
        profile = data.get("UserProfile") or {}
        login = profile.get("LoginName")
        if not isinstance(node_id, str) or not node_id:
            return None
        return PeerIdentity(node_id, login if isinstance(login, str) else None)
    except (ValueError, KeyError, TypeError):
        return None


def _address(host: str, port: int) -> str:
    if port == 0:  # an address without a connection, used when pairing
        return host
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("local-tailscaled.sock", timeout=TIMEOUT_SECONDS)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(self._path)
        self.sock = sock


class LocalApiWhois:
    def __init__(self, socket_path: Path) -> None:
        self._socket = str(socket_path)

    def lookup(self, host: str, port: int) -> PeerIdentity | None:
        try:
            connection = _UnixConnection(self._socket)
            try:
                connection.request(
                    "GET",
                    "/localapi/v0/whois?addr=" + quote(_address(host, port)),
                    headers={"Host": "local-tailscaled.sock"},
                )
                response = connection.getresponse()
                body = response.read(1 << 20)
            finally:
                connection.close()
        except (OSError, http.client.HTTPException):
            logger.warning("whois unavailable: localapi")
            raise WhoisUnavailable from None
        if response.status == 404:
            return None  # tailscaled answered: no such peer
        if response.status != 200:
            raise WhoisUnavailable
        identity = parse_whois(body)
        if identity is None:
            raise WhoisUnavailable
        return identity


class CliWhois:
    def __init__(self, executable: Path | None = None) -> None:
        if executable is not None:
            self._executable = str(executable)
        elif APP_BUNDLE_CLI.is_file():
            self._executable = str(APP_BUNDLE_CLI)
        else:
            self._executable = "tailscale"

    def lookup(self, host: str, port: int) -> PeerIdentity | None:
        try:
            done = subprocess.run(
                [self._executable, "whois", "--json", _address(host, port)],
                capture_output=True,
                timeout=TIMEOUT_SECONDS + 2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("whois unavailable: cli")
            raise WhoisUnavailable from None
        if done.returncode != 0:
            if b"no match" in done.stderr.lower():
                return None  # tailscaled answered: unknown address
            raise WhoisUnavailable
        identity = parse_whois(done.stdout)
        if identity is None:
            raise WhoisUnavailable
        return identity


class CachedWhois:
    """Remember successful answers per peer ``ip:port`` for a few seconds.

    Only positive answers are cached, so a revoked or removed node is seen again
    within ``ttl`` seconds and failures are never remembered. The bearer token is
    not part of the key.
    """

    def __init__(
        self,
        inner: Whois,
        ttl: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[tuple[str, int], tuple[float, PeerIdentity]] = {}

    def lookup(self, host: str, port: int) -> PeerIdentity | None:
        now = self._clock()
        key = (host, port)
        hit = self._entries.get(key)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        self._entries = {
            k: v for k, v in self._entries.items() if now - v[0] < self._ttl
        }
        identity = self._inner.lookup(host, port)
        if identity is not None:
            self._entries[key] = (now, identity)
        return identity
