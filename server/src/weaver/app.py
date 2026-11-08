"""Typer CLI: resident runtime and its user LaunchAgent (#16).

Imports stay free of configuration, user data and threads; every command
imports what it needs lazily.
"""

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from weaver.config import ServerConfig

cli = typer.Typer(add_completion=False, pretty_exceptions_enable=False)

ConfigOption = Annotated[
    str | None, typer.Option("--config", help="TOML file (overrides WEAVER_CONFIG)")
]


EX_CONFIG = 78  # sysexits.h: configuration error


def _load(config: str | None, *, init_directories: bool = False) -> "ServerConfig":
    """Configuration errors exit 78 (EX_CONFIG) with field names only.

    Under launchd (WEAVER_SUPERVISED=1) the process first waits, because
    KeepAlive cannot exclude an exit code and would otherwise restart a
    broken configuration every ThrottleInterval.
    """
    import os

    from weaver.config import ConfigError, load_config

    try:
        return load_config(config, init_directories=init_directories)
    except ConfigError as error:
        typer.echo(f"Error: invalid configuration: {error}", err=True)
        from weaver.runtime import launchd

        if os.environ.get(launchd.SUPERVISED_ENV) == "1":
            import time

            time.sleep(launchd.CONFIG_ERROR_HOLD_SECONDS)
        raise typer.Exit(code=EX_CONFIG) from None


@cli.command()
def run(
    config: ConfigOption = None,
    check_config: Annotated[
        bool, typer.Option("--check-config", help="Validate settings and exit")
    ] = False,
    init_directories: Annotated[
        bool,
        typer.Option(
            "--init-directories",
            help="Validate, create missing data directories, and exit",
        ),
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", min=0.5, help="Rescan interval seconds")
    ] = 5.0,
) -> None:
    """Run Weaver in the foreground until SIGTERM (launchd-friendly)."""
    settings = _load(config, init_directories=init_directories)
    if check_config or init_directories:
        typer.echo("Configuration valid.")
        return

    import logging

    from weaver.runtime import EXIT_ALREADY_RUNNING, AlreadyRunning, InstanceLock
    from weaver.runtime.logs import configure_logging

    log_file = configure_logging(settings.log_root)
    logger = logging.getLogger("weaver.runtime")
    try:
        # Lock before opening any store so a duplicate never touches state.
        with InstanceLock(settings.state_root / "weaver.lock"):
            from weaver.runtime.assemble import build_runtime

            try:
                runtime = build_runtime(settings, interval=interval)
            except Exception as error:
                logger.critical("runtime setup failed: %s", type(error).__name__)
                typer.echo(
                    f"Error: runtime setup failed ({type(error).__name__}); "
                    f"see {log_file}",
                    err=True,
                )
                raise typer.Exit(code=1) from None
            code = runtime.serve(acquire_lock=False)
    except AlreadyRunning:
        logger.error("another weaver instance holds the state lock; exiting")
        typer.echo("Error: another weaver instance is running", err=True)
        raise typer.Exit(code=EXIT_ALREADY_RUNNING) from None
    if code:
        raise typer.Exit(code=code)


@cli.command("install-agent")
def install_agent(
    config: ConfigOption = None,
    python: Annotated[
        str | None,
        typer.Option(
            "--python", help="Absolute interpreter (default: this one, unresolved)"
        ),
    ] = None,
    print_only: Annotated[
        bool, typer.Option("--print", help="Print the plist without installing")
    ] = False,
) -> None:
    """Install and start the user LaunchAgent (~/Library/LaunchAgents)."""
    from weaver.runtime import launchd

    settings = _load(config)
    interpreter = Path(python) if python else Path(sys.executable).absolute()
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise typer.BadParameter("python: expected an existing absolute path")
    for name, directory in (
        ("state_root", settings.state_root),
        ("log_root", settings.log_root),
    ):
        if not directory.is_dir():
            typer.echo(f"Error: {name} does not exist; run --init-directories")
            raise typer.Exit(code=EX_CONFIG)
    plist = launchd.render_plist(
        python=interpreter,
        config_file=settings.config_file,
        log_root=settings.log_root,
        working_directory=settings.state_root,
    )
    _warn_llm_under_launchd(settings, plist)
    if print_only:
        typer.echo(plist.decode("utf-8"), nl=False)
        return
    try:
        target = launchd.install(
            plist, home=Path.home(), launchctl=launchd.run_launchctl
        )
    except launchd.LaunchdError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Installed {target}")


def _warn_llm_under_launchd(settings: "ServerConfig", plist: bytes) -> None:
    """launchd passes only the plist's EnvironmentVariables, not the shell's."""
    import plistlib

    from weaver.runtime.pipeline import llm_status

    environment = plistlib.loads(plist).get("EnvironmentVariables", {})
    llm = llm_status(settings.llm, environment)
    if not llm.available:
        typer.echo(
            f"Warning: under launchd the LLM stage will be skipped ({llm.reason}); "
            "set llm.api_key_file to a private key file (chmod 600).",
            err=True,
        )


@cli.command("uninstall-agent")
def uninstall_agent() -> None:
    """Stop the LaunchAgent and remove its plist (logs and state are kept)."""
    from weaver.runtime import launchd

    try:
        removed = launchd.uninstall(home=Path.home(), launchctl=launchd.run_launchctl)
    except launchd.LaunchdError as error:
        typer.echo(f"Error: {error}; plist kept", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Removed LaunchAgent." if removed else "LaunchAgent not installed.")


@cli.command()
def status(config: ConfigOption = None) -> None:
    """Show launchd state and the running instance's last status report."""
    from weaver.runtime import launchd

    settings = _load(config)
    typer.echo(f"launchd: {launchd.status(launchctl=launchd.run_launchctl)}")
    report = settings.state_root / "runtime-status.json"
    try:
        typer.echo(report.read_text(encoding="utf-8"), nl=False)
    except FileNotFoundError:
        typer.echo("runtime: no status report yet")
    typer.echo(f"\nlog: {settings.log_root / 'weaver.log'}")


def main(argv: Sequence[str] | None = None) -> int:
    """Invoke the CLI, retaining a return code for successful programmatic calls."""
    try:
        cli(
            args=list(argv) if argv is not None else None,
            prog_name="python -m weaver.app",
        )
    except SystemExit as error:
        if error.code != 0:
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
