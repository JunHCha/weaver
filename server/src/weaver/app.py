"""Typer CLI for server configuration; resident runtime follows in later issues."""

from collections.abc import Sequence
from typing import Annotated

import typer

cli = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


@cli.command()
def run(
    config: Annotated[
        str | None, typer.Option("--config", help="TOML file (overrides WEAVER_CONFIG)")
    ] = None,
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
) -> None:
    """Weaver server (Spinner and Loom). Runtime is not implemented yet."""
    # Keep --help and imports free from configuration and user-data access.
    from weaver.config import ConfigError, load_config

    try:
        load_config(config, init_directories=init_directories)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from None
    if check_config or init_directories:
        typer.echo("Configuration valid.")
        return
    typer.echo("Error: server runtime is not implemented yet", err=True)
    raise typer.Exit(code=2)


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
