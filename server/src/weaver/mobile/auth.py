"""Two-layer authentication in front of every phone API route.

1. The exact ``Host`` must be the configured tailnet ``bind:port``; a request
   carrying an ``Origin`` (any browser) is refused; there is no CORS or cookie.
2. tailscaled ``whois`` for the real socket peer must name a node. Unknown or
   unverifiable peers are refused (fail closed) before the token is looked at.
3. The bearer token must belong to an active device bound to that same node.

Only the dedicated Shepherd loopback app replaces whois with an explicit
loopback peer/listener check and a registered local capture-only credential.
The tailnet app always requires whois; tokens cannot cross these listeners.

Every refusal is a fixed code. Nothing request-controlled reaches a log.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
from starlette.types import ASGIApp, Receive, Scope, Send

from weaver.shepherd.registry import Registry

from .store import Device, MobileStore
from .whois import Whois, WhoisUnavailable

logger = logging.getLogger("weaver.mobile")
PREFIX = "/mobile/v1"


@dataclass(frozen=True)
class Principal:
    device: Device


async def _respond(
    send: Send,
    status: int,
    code: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    retryable: bool = False,
) -> None:
    body = json.dumps({"code": code, "retryable": retryable}).encode()
    if retryable:
        headers = [*(headers or []), (b"retry-after", b"2")]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        store: MobileStore,
        whois: Whois | None,
        allowed_hosts: frozenset[str],
        local_bind: tuple[str, int] | None = None,
    ) -> None:
        if local_bind is not None and local_bind[0] not in ("127.0.0.1", "::1"):
            raise ValueError("invalid_loopback_bind")
        if local_bind is None and whois is None:
            raise ValueError("tailnet_requires_whois")
        self.local_bind = local_bind
        self.app = app
        self.store = store
        self.whois = whois
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path: str = scope["path"]
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            await _respond(send, 404, "not_found")
            return
        raw_headers = scope["headers"]
        for name in (b"host", b"authorization"):
            if sum(k.lower() == name for k, _ in raw_headers) > 1:
                await _respond(send, 400, "invalid_headers")
                return
        headers = {k.lower(): v for k, v in raw_headers}
        host = headers.get(b"host", b"").decode("latin-1").lower()
        if host not in self.allowed_hosts:
            await _respond(send, 400, "invalid_host")
            return
        if b"origin" in headers:
            await _respond(send, 403, "origin_not_allowed")
            return
        client = scope.get("client")
        if not client:
            await _respond(send, 403, "peer_unverified")
            return
        identity = None
        if self.local_bind is not None:
            # Both real peer and listener must be loopback; ignore forwarded headers.
            if (
                client[0] not in ("127.0.0.1", "::1")
                or tuple(scope.get("server") or ()) != self.local_bind
            ):
                await _respond(send, 403, "socket_mismatch")
                return
        else:
            assert self.whois is not None
            try:
                identity = await anyio.to_thread.run_sync(
                    self.whois.lookup, str(client[0]), int(client[1])
                )
            except WhoisUnavailable:
                logger.info("mobile deferred: whois_unavailable")
                await _respond(send, 503, "whois_unavailable", retryable=True)
                return
            if identity is None:
                logger.info("mobile refused: peer_unverified")
                await _respond(send, 403, "peer_unverified")
                return
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = authorization.partition(" ")
        challenge = [(b"www-authenticate", b"Bearer")]
        if scheme.lower() != "bearer" or not token or not token.isascii():
            await _respond(send, 401, "invalid_token", headers=challenge)
            return
        device = await anyio.to_thread.run_sync(self.store.device_for_token, token)
        if device is None:
            await _respond(send, 401, "invalid_token", headers=challenge)
            return
        if not device.active:
            logger.info("mobile refused: token_revoked %s", device.device_id)
            await _respond(send, 401, "token_revoked", headers=challenge)
            return
        transport = await anyio.to_thread.run_sync(
            Registry(self.store).transport, device.device_id
        )
        if self.local_bind is not None:
            if transport != "loopback" or device.scopes != frozenset({"capture"}):
                await _respond(send, 403, "socket_mismatch")
                return
        elif transport == "loopback":
            await _respond(send, 403, "socket_mismatch")
            return
        elif (
            identity is None
            or device.node_id != identity.node_id
            or (
                device.login_name is not None
                and device.login_name != identity.login_name
            )
        ):
            logger.info("mobile refused: node_mismatch %s", device.device_id)
            await _respond(send, 403, "node_mismatch")
            return
        if self.local_bind is not None and not (
            (
                scope["method"] == "PUT"
                and re.fullmatch(r"/mobile/v1/captures/[^/]+/(payload|manifest)", path)
            )
            or (
                scope["method"] == "GET"
                and re.fullmatch(r"/mobile/v1/captures/[^/]+/receipt", path)
            )
        ):
            await _respond(send, 403, "scope_denied")
            return
        state: dict[str, Any] = scope.setdefault("state", {})
        state["principal"] = Principal(device)
        await self.app(scope, receive, send)
