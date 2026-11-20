"""Local operator registration and quarantine recovery; tokens never printed."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from weaver.config import ServerConfig


def _load(config: str | None) -> ServerConfig:
    from weaver.app import _load as load

    return load(config)


shepherd_cli = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
ConfigOption = Annotated[str | None, typer.Option("--config")]


def private_path(path: Path, settings: ServerConfig) -> Path:
    lexical = path.expanduser().absolute()
    resolved = lexical.resolve()
    if lexical != resolved:
        raise ValueError("unsafe_private_path")
    roots = [settings.sync_root, settings.vault_root]
    if settings.wiki.vault_root is not None:
        roots.append(settings.wiki.vault_root)
    if any(
        resolved.parent.is_relative_to(root) or root.is_relative_to(resolved.parent)
        for root in roots
    ):
        raise ValueError("private_path_overlaps_sync_or_vault")
    return resolved


@shepherd_cli.command()
def register(
    instance_id: str,
    token_file: Annotated[Path, typer.Option("--token-file")],
    config: ConfigOption = None,
    transport: Annotated[str, typer.Option("--transport")] = "loopback",
    node_id: Annotated[str | None, typer.Option("--node-id")] = None,
    login_name: Annotated[str | None, typer.Option("--login-name")] = None,
) -> None:
    """Create/rotate a capture-only token; retain instance and capture IDs."""
    from weaver.mobile.store import MobileStore
    from weaver.private import PrivateFileError

    from .registry import Registry

    settings = _load(config)
    try:
        if transport not in ("loopback", "tailnet"):
            raise ValueError("invalid_shepherd_transport")
        device = Registry(MobileStore(settings.state_root)).register(
            instance_id,
            private_path(token_file, settings),
            transport="loopback" if transport == "loopback" else "tailnet",
            node_id=node_id,
            login_name=login_name,
        )
    except (ValueError, PrivateFileError, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Registered {instance_id}: {device.device_id} capture {transport}")


@shepherd_cli.command()
def revoke(instance_id: str, config: ConfigOption = None) -> None:
    """Revoke immediately; keep instance namespace and all pending captures."""
    from weaver.mobile.store import MobileStore

    from .registry import Registry

    if not Registry(MobileStore(_load(config).state_root)).revoke(instance_id):
        typer.echo("Error: no active instance", err=True)
        raise typer.Exit(1)
    typer.echo(f"Revoked {instance_id}")


@shepherd_cli.command()
def serve(config: ConfigOption = None) -> None:
    """Run a separate loopback capture listener; no tailnet whois bypass."""
    import uvicorn

    from weaver.mobile.api import create_app

    os.umask(0o077)
    settings = _load(config)
    local = settings.shepherds.courier
    app = create_app(settings, None, local_bind=(local.bind, local.port))
    uvicorn.run(
        app,
        host=local.bind,
        port=local.port,
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_level="warning",
        ws="none",
    )


@shepherd_cli.command()
def outbox(
    instance_id: str,
    config: ConfigOption = None,
    action: Annotated[
        str, typer.Option("--action", help="status, retry, discard, resume, flush")
    ] = "status",
    capture_id: Annotated[str | None, typer.Option("--capture-id")] = None,
    token_file: Annotated[Path | None, typer.Option("--token-file")] = None,
) -> None:
    """Inspect or explicitly repair a private spool; flush targets loopback only.

    Retry preserves immutable bytes; new revisions must be enqueued separately.
    Discard permanently removes only the selected local copy, never source data.
    Resume after registration repairs auth; Receipt recovery precedes uploads.
    """
    import re

    import httpx

    from weaver.private import PrivateFile, PrivateFileError

    from .outbox import Outbox

    settings = _load(config)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", instance_id):
        raise typer.BadParameter("invalid_instance_id")
    box = Outbox(
        settings.state_root / "shepherds" / instance_id / "outbox.sqlite3",
        instance_id,
        settings.shepherds.courier.limits,
    )
    try:
        if action == "status":
            typer.echo(f"auth_paused={box.paused}")
            for entry in box.entries():
                typer.echo(
                    f"{entry.capture_id}\t{entry.state}\t{entry.error or '-'}\t{entry.depends_on or '-'}"
                )
        elif action in ("retry", "discard") and capture_id:
            getattr(box, action)(capture_id)
        elif action == "resume":
            box.resume()
        elif action == "flush" and token_file:
            private = PrivateFile(private_path(token_file, settings))
            with private.locked() as parent:
                raw = private.read(parent)
            if raw is None:
                raise ValueError("token_file_missing")
            token = raw.decode("ascii").strip()
            if not token.startswith("wvr_") or any(c.isspace() for c in token):
                raise ValueError("invalid_token_file")
            local = settings.shepherds.courier
            host = f"[{local.bind}]" if ":" in local.bind else local.bind
            with httpx.Client(
                base_url=f"http://{host}:{local.port}/mobile/v1/",
                headers={"Authorization": f"Bearer {token}"},
                trust_env=False,
                timeout=30,
            ) as client:
                box.deliver(client)
        else:
            raise ValueError("invalid_action_or_missing_argument")
    except (ValueError, PrivateFileError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from None
