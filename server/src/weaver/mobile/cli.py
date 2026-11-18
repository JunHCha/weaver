"""``weaver mobile``: serve the phone API and manage paired devices (Typer)."""

import base64
import json
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from weaver.config import ServerConfig
    from weaver.mobile.whois import Whois

mobile_cli = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Phone HTTPS API over Tailscale (ADR-040).",
)
ConfigOption = Annotated[
    str | None, typer.Option("--config", help="TOML file (overrides WEAVER_CONFIG)")
]


def _settings(config: str | None, *, need_bind: bool = True) -> "ServerConfig":
    from weaver.app import _load

    settings = _load(config)
    if need_bind and settings.mobile.bind is None:
        typer.echo("Error: [mobile] bind is not configured", err=True)
        raise typer.Exit(code=78)
    return settings


def build_whois(settings: "ServerConfig") -> "Whois":
    from weaver.mobile.whois import CachedWhois, CliWhois, LocalApiWhois

    mobile = settings.mobile
    if mobile.whois == "cli":
        return CachedWhois(CliWhois(mobile.tailscale_cli))
    return CachedWhois(LocalApiWhois(mobile.whois_socket))


@mobile_cli.command()
def serve(config: ConfigOption = None) -> None:
    """Listen on the configured tailnet address only, over HTTPS.

    Fails closed: if the address is not assigned (tailscaled down or the
    address changed) the listener does not start; there is no wildcard fallback.
    """
    import os

    os.umask(0o077)
    import uvicorn

    from weaver.action import ActionOutbox
    from weaver.action.courier import ActionCourier
    from weaver.fabric import FabricStore
    from weaver.mobile.api import create_app
    from weaver.mobile.tls import ensure_certificates

    settings = _settings(config)
    mobile = settings.mobile
    assert mobile.bind is not None
    files = ensure_certificates(settings.state_root / "mobile" / "tls", mobile.bind)
    whois = build_whois(settings)
    actions = ActionCourier(
        settings.sync_root,
        ActionOutbox(settings.state_root / "action-outbox.sqlite3"),
        FabricStore.from_config(settings),
        publish_files=lambda: mobile.transport_for("action") == "files",
    )
    app = create_app(settings, whois, actions=actions)
    uvicorn.run(
        app,
        host=mobile.bind,
        port=mobile.port,
        ssl_certfile=str(files.server_cert),
        ssl_keyfile=str(files.server_key),
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_level="warning",
        ws="none",
    )


@mobile_cli.command()
def pair(
    label: Annotated[str, typer.Option("--label", help="Device name, e.g. fold")],
    config: ConfigOption = None,
    node_id: Annotated[
        str | None, typer.Option("--node-id", help="Tailscale node StableID")
    ] = None,
    phone_ip: Annotated[
        str | None,
        typer.Option("--phone-ip", help="Resolve the node ID through whois"),
    ] = None,
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", help="read, capture, review, action, edit (repeat)"),
    ] = None,
    executor: Annotated[
        bool, typer.Option("--executor", help="Make this the one Action executor")
    ] = False,
    qr: Annotated[bool, typer.Option("--qr/--no-qr")] = True,
    print_json: Annotated[
        bool,
        typer.Option(
            "--print-json",
            help="Also print the pairing payload (it contains the token) as text",
        ),
    ] = False,
) -> None:
    """Issue a token for one phone and print the pairing QR (shown once)."""
    from weaver.mobile.store import DeviceError, MobileStore
    from weaver.mobile.tls import ensure_certificates

    if not qr and not print_json:
        raise typer.BadParameter("--no-qr needs --print-json (the token is shown once)")
    settings = _settings(config)
    mobile = settings.mobile
    assert mobile.bind is not None
    if (node_id is None) == (phone_ip is None):
        raise typer.BadParameter("choose exactly one of --node-id or --phone-ip")
    login: str | None = None
    if phone_ip is not None:
        identity = build_whois(settings).lookup(phone_ip, 0)
        if identity is None:
            typer.echo("Error: whois could not identify that address", err=True)
            raise typer.Exit(code=1)
        node_id, login = identity.node_id, identity.login_name
    assert node_id is not None
    scopes = frozenset(scope or ["read", "capture", "review", "edit"])
    if executor:
        scopes |= {"action"}
    store = MobileStore(settings.state_root)
    try:
        device, token = store.create_device(
            label, node_id, scopes, login_name=login, executor=executor
        )
    except DeviceError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from None
    files = ensure_certificates(settings.state_root / "mobile" / "tls", mobile.bind)
    host = f"[{mobile.bind}]" if ":" in mobile.bind else mobile.bind
    payload = json.dumps(
        {
            "v": 1,
            "server_url": f"https://{host}:{mobile.port}",
            "server_id": store.server_id,
            "device_id": device.device_id,
            "token": token,
            "ca_sha256": files.ca_sha256,
            "ca_der_b64": base64.b64encode(files.ca_der).decode(),
        },
        separators=(",", ":"),
    )
    typer.echo(f"device: {device.device_id} node: {node_id}")
    typer.echo("The token is shown once. Scan the QR code with the Weaver app.")
    if qr:
        import segno

        segno.make(payload, error="l").terminal(compact=True)
    if print_json:
        typer.echo(payload)


@mobile_cli.command()
def devices(config: ConfigOption = None) -> None:
    """List paired devices (never tokens)."""
    from weaver.mobile.store import MobileStore

    settings = _settings(config, need_bind=False)
    for device in MobileStore(settings.state_root).devices():
        state = "revoked " + device.revoked_at if device.revoked_at else "active"
        typer.echo(
            f"{device.device_id}\t{device.label}\t{state}\t"
            f"{','.join(sorted(device.scopes))}\t"
            f"{'executor' if device.executor else ''}"
        )


@mobile_cli.command()
def revoke(device_id: str, config: ConfigOption = None) -> None:
    """Revoke a device's token; its queued work stays in the journal."""
    from weaver.mobile.store import MobileStore

    settings = _settings(config, need_bind=False)
    if not MobileStore(settings.state_root).revoke(device_id):
        typer.echo("Error: no active device with that ID", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Revoked {device_id}")


@mobile_cli.command()
def cert(config: ConfigOption = None) -> None:
    """Create or renew the local CA/server certificate and print the CA digest."""
    from weaver.mobile.tls import ensure_certificates

    settings = _settings(config)
    assert settings.mobile.bind is not None
    files = ensure_certificates(
        settings.state_root / "mobile" / "tls", settings.mobile.bind
    )
    typer.echo(f"ca_sha256: {files.ca_sha256}")
    typer.echo(f"ca: {files.ca_cert}")


@mobile_cli.command()
def status(config: ConfigOption = None) -> None:
    """Show per-feature transports and unfinished phone operations."""
    from weaver.config import TRANSPORT_FEATURES
    from weaver.mobile.store import MobileStore

    settings = _settings(config, need_bind=False)
    store = MobileStore(settings.state_root)
    for feature in TRANSPORT_FEATURES:
        typer.echo(
            f"{feature}: configured={settings.mobile.transport_for(feature)} "
            f"recorded={store.recorded_transport(feature) or '-'}"
        )
    for kind in ("verdict", "action_result"):
        typer.echo(f"pending {kind}: {len(store.pending(kind))}")
    typer.echo(f"server_id: {store.server_id}")


@mobile_cli.command()
def reconcile(config: ConfigOption = None) -> None:
    """Apply a transport change now: republish unfinished items when back on files."""
    from weaver.action import ActionOutbox
    from weaver.action.courier import ActionCourier
    from weaver.fabric import FabricStore
    from weaver.loom.summary_state import SummaryState
    from weaver.mobile.reviews import ReviewService
    from weaver.mobile.store import MobileStore
    from weaver.mobile.transport import reconcile_transports

    settings = _settings(config, need_bind=False)
    store = MobileStore(settings.state_root)
    actions = ActionCourier(
        settings.sync_root,
        ActionOutbox(settings.state_root / "action-outbox.sqlite3"),
        FabricStore.from_config(settings),
        publish_files=lambda: settings.mobile.transport_for("action") == "files",
    )
    reviews = ReviewService(SummaryState(settings.state_root), store)
    changed = reconcile_transports(settings.mobile, store, reviews, actions, settings)
    typer.echo("republished: " + (", ".join(changed) if changed else "nothing"))
