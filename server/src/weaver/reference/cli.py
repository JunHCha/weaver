"""``weaver reference``: sync and inspect the reference index."""

import json
from dataclasses import asdict
from typing import Annotated

import typer

reference_cli = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Reference vault sync and lookup.",
)
ConfigOption = Annotated[
    str | None, typer.Option("--config", help="TOML file (overrides WEAVER_CONFIG)")
]


@reference_cli.command("sync")
def sync(
    config: ConfigOption = None,
    sphere: Annotated[str | None, typer.Option("--sphere")] = None,
    full: Annotated[bool, typer.Option("--full", help="Reindex everything")] = False,
    reclone: Annotated[
        bool, typer.Option("--reclone", help="Delete and clone again")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    from weaver.app import _load
    from weaver.reference import ReferenceSyncError, sync_all

    settings = _load(config)
    try:
        reports = sync_all(settings, sphere=sphere, full=full, reclone=reclone)
    except ReferenceSyncError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from None
    if as_json:
        typer.echo(json.dumps([asdict(r) for r in reports], ensure_ascii=False))
        return
    for r in reports:
        typer.echo(
            f"{r.sphere}: indexed={r.indexed} removed={r.removed} "
            f"skipped={r.skipped} full={r.full} commit={r.commit or '-'}"
        )


@reference_cli.command("find")
def find(
    config: ConfigOption = None,
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    query: Annotated[str, typer.Option("--query")] = "",
    limit: Annotated[int, typer.Option("--limit")] = 5,
) -> None:
    from weaver.app import _load
    from weaver.reference import find_references

    settings = _load(config)
    try:
        hits = find_references(settings.state_root, tag or [], query, limit)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(2) from None
    for hit in hits:
        typer.echo(
            f"[{hit.match}] {hit.sphere}/{hit.path} {hit.heading} #{hit.ordinal}"
        )
        typer.echo("    " + hit.text[:200].replace("\n", " "))
