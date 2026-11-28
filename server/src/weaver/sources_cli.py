"""Source maintenance commands; preview is always the default."""

import sqlite3
from collections import Counter
from datetime import datetime
from typing import Annotated

import typer

from weaver.runtime.source_retry import RetryTarget, reset_targets, select_targets

sources_cli = typer.Typer(add_completion=False, help="Inspect and recover Sources.")


def _date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except ValueError:
        raise typer.BadParameter("dates require ISO 8601 with a timezone") from None


def _preview(targets: list[RetryTarget], limit: int | None) -> None:
    typer.echo(f"Eligible Sources: {len(targets)} (limit: {limit or 'none'})")
    for reason, count in sorted(
        Counter(target.error_class or "(none)" for target in targets).items()
    ):
        typer.echo(f"  {reason}: {count}")
    if targets:
        dates = [target.captured_at for target in targets]
        typer.echo(
            f"Captured range: {min(dates).isoformat()} .. {max(dates).isoformat()}"
        )
    else:
        typer.echo("Captured range: (empty)")
    typer.echo("Order: archive arrival (oldest first); dates filter captured_at.")
    for target in targets:
        typer.echo(f"  {target.source_id}")


@sources_cli.command("retry")
def retry(
    config: Annotated[str | None, typer.Option("--config")] = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    error_class: Annotated[str | None, typer.Option("--error-class")] = None,
    status: Annotated[str, typer.Option("--status")] = "failed",
    since: Annotated[
        str | None, typer.Option("--since", help="Inclusive captured_at, ISO timezone")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="Exclusive captured_at, ISO timezone")
    ] = None,
    source_ids: Annotated[list[str] | None, typer.Option("--source-id")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
) -> None:
    """Preview eligible Sources; --apply also requires interactive confirmation.

    Stop the server before applying. Completed Sources and any Source with a
    review package are excluded. LLM daily limits remain in force after restart.
    """
    from weaver.app import _load
    from weaver.runtime import EXIT_ALREADY_RUNNING, AlreadyRunning, InstanceLock

    settings = _load(config)
    start, end = _date(since), _date(until)

    def select() -> list[RetryTarget]:
        return select_targets(
            settings.state_root,
            settings.archive_root,
            status=status,
            error_class=error_class,
            since=start,
            until=end,
            source_ids=tuple(source_ids or ()),
            limit=limit,
        )

    try:
        if not apply:
            _preview(select(), limit)
            typer.echo("Preview only. Use --apply to reset runtime progress.")
            return
        with InstanceLock(settings.state_root / "weaver.lock"):
            targets = select()
            _preview(targets, limit)
            if not targets:
                return
            typer.confirm("Reset these Sources for retry?", abort=True)
            count = reset_targets(settings.state_root, targets)
            typer.echo(f"Reset {count} Sources. Start the server to process them.")
    except AlreadyRunning:
        typer.echo("Error: stop the Weaver server before applying retries.", err=True)
        raise typer.Exit(EXIT_ALREADY_RUNNING) from None
    except (ValueError, OSError, sqlite3.Error) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from None
