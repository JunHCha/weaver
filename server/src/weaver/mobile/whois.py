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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

logger = logging.getLogger("weaver.mobile")
TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class PeerIdentity:
    node_id: str
    login_name: str | None


class Whois(Protocol):
    def lookup(self, host: str, port: int) -> PeerIdentity | None: ...


def parse_whois(raw: bytes) -> PeerIdentity | None:
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
                if response.status != 200:
                    return None
            finally:
                connection.close()
        except (OSError, http.client.HTTPException):
            logger.warning("whois unavailable: localapi")
            return None
        return parse_whois(body)


class CliWhois:
    def __init__(self, executable: Path | None = None) -> None:
        self._executable = str(executable) if executable else "tailscale"

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
            return None
        return parse_whois(done.stdout) if done.returncode == 0 else None
