"""Typer CLI: resident runtime and its user LaunchAgent (#16).

Imports stay free of configuration, user data and threads; every command
imports what it needs lazily.
"""

import sys
from collections.abc import Sequence
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from weaver.mobile.cli import mobile_cli
from weaver.webex.cli import webex_cli

if TYPE_CHECKING:
    from weaver.config import ServerConfig

cli = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
cli.add_typer(mobile_cli, name="mobile")
cli.add_typer(webex_cli, name="webex")

ConfigOption = Annotated[
    str | None, typer.Option("--config", help="TOML file (overrides WEAVER_CONFIG)")
]


class LlmBackend(StrEnum):
    anthropic = "anthropic"
    claude_cli = "claude_cli"
    codex_cli = "codex_cli"


LlmOption = Annotated[
    LlmBackend | None,
    typer.Option("--llm", help="Override llm.backend for this invocation"),
]


EX_CONFIG = 78  # sysexits.h: configuration error


def _load(
    config: str | None, *, init_directories: bool = False, llm: LlmBackend | None = None
) -> "ServerConfig":
    """Configuration errors exit 78 (EX_CONFIG) with field names only.

    Under launchd (WEAVER_SUPERVISED=1) the process first waits, because
    KeepAlive cannot exclude an exit code and would otherwise restart a
    broken configuration every ThrottleInterval.
    """
    import os

    from weaver.config import ConfigError, load_config

    try:
        settings = load_config(config, init_directories=init_directories)
        if llm is not None:
            settings = replace(settings, llm=replace(settings.llm, backend=llm.value))
        return settings
    except ConfigError as error:
        typer.echo(f"Error: invalid configuration: {error}", err=True)
        from weaver.runtime import launchd

        if os.environ.get(launchd.SUPERVISED_ENV) == "1":
            import time

            time.sleep(launchd.CONFIG_ERROR_HOLD_SECONDS)
        raise typer.Exit(code=EX_CONFIG) from None


@cli.command("policy-backlog")
def policy_backlog(
    config: ConfigOption = None,
    source_ids: Annotated[
        list[str] | None, typer.Option("--source-id", help="Exact Source IDs to review")
    ] = None,
    resume: Annotated[bool, typer.Option("--resume")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Show held scope/count/dates; resume only explicitly selected Sources."""
    import json

    from weaver.llm.ledger import Ledger
    from weaver.llm.policy import PolicyBlocked, Provenance
    from weaver.llm.policy_archive import configured_policy

    settings = _load(config)
    ledger = Ledger(settings.state_root / "llm.sqlite3")
    rows = ledger.policy_backlog()
    selected = set(source_ids or [])
    reviewed = [(sid, at) for sid, at in rows if not selected or sid in selected]
    if selected - {sid for sid, _ in rows}:
        typer.echo("Error: unknown backlog Source ID", err=True)
        raise typer.Exit(2)
    typer.echo(
        json.dumps(
            {
                "count": len(reviewed),
                "first_blocked_at": min((at for _, at in reviewed), default=None),
                "last_blocked_at": max((at for _, at in reviewed), default=None),
                "sources": [
                    {"source_id": sid, "blocked_at": at} for sid, at in reviewed
                ],
            }
        )
    )
    if not resume:
        return
    if not selected or not reviewed:
        typer.echo("Error: --resume requires explicit --source-id selection", err=True)
        raise typer.Exit(2)
    if not yes and (
        not sys.stdin.isatty()
        or not typer.confirm("표시한 출처·기간·건수의 LLM 대기 작업을 재개합니까?")
    ):
        raise typer.Exit(2)
    try:
        # Approval does not override a closed/unknown channel or excluded Space.
        configured_policy(settings).check(Provenance(frozenset(selected)))
        ledger.policy_resume(reviewed)
    except (PolicyBlocked, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(2) from None
    typer.echo(json.dumps({"resumed": len(reviewed)}))


@cli.command("wiki-measure")
def wiki_measure(
    config: ConfigOption = None,
    after_seq: Annotated[
        int | None,
        typer.Option(
            "--after-seq",
            min=0,
            help="Replay from a sequence instead of the saved bookmark",
        ),
    ] = None,
) -> None:
    """Report full/partial prompt estimates without models or state changes."""
    import json

    from weaver.context import ContextDictionary
    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.schema import EventSummary
    from weaver.wiki.measurement import bookmark, measure
    from weaver.wiki.store import WikiStore

    settings = _load(config)
    store = WikiStore.open_readonly(
        settings.wiki.vault_root or settings.vault_root,
        settings.state_root,
        wiki_root=settings.wiki.folder,
    )
    context = ContextDictionary.open_readonly(settings.state_root / "context.sqlite3")
    fabric = FabricStore.open_readonly(settings.state_root / "fabric.sqlite3")
    summaries: dict[str, EventSummary] = {}
    for path in sorted((settings.archive_root / "summaries").glob("*/*.json")):
        summary = EventSummary.model_validate_json(path.read_bytes())
        previous = summaries.get(summary.source_id)
        if previous is None or summary.created_at > previous.created_at:
            summaries[summary.source_id] = summary
    result = measure(
        store,
        fabric.entries(ThreadQuery()),
        context.snapshot(),
        after_seq=after_seq
        if after_seq is not None
        else bookmark(settings.state_root / "wiki-compiler.sqlite3"),
        sources={key: value.body for key, value in summaries.items()},
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@cli.command("wiki-migrate")
def wiki_migrate(
    config: ConfigOption = None,
    preview: Annotated[
        Path | None, typer.Option("--preview", help="Write a reviewable JSON preview")
    ] = None,
    apply: Annotated[
        Path | None, typer.Option("--apply", help="Apply this reviewed preview")
    ] = None,
    backup: Annotated[
        Path | None,
        typer.Option("--backup", help="New directory outside vault and state"),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm the reviewed migration")
    ] = False,
) -> None:
    """Separate Wiki metadata; applying requires a preview, backup and confirmation."""
    import json

    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.wiki.migration import WikiMigration
    from weaver.wiki.store import WikiStore

    if (preview is None) == (apply is None):
        raise typer.BadParameter("choose exactly one of --preview or --apply")
    if apply is not None and (backup is None or not yes):
        raise typer.BadParameter(
            "--apply requires --backup and --yes after reviewing the preview"
        )
    settings = _load(config)
    store = WikiStore(
        settings.wiki.vault_root or settings.vault_root,
        settings.state_root,
        wiki_root=settings.wiki.folder,
        settle_seconds=settings.wiki.settle_seconds,
    )
    fabric_path = settings.state_root / "fabric.sqlite3"
    threads = (
        FabricStore.open_readonly(fabric_path).entries(ThreadQuery())
        if fabric_path.exists()
        else []
    )
    from weaver.context import ContextDictionary

    context_path = settings.state_root / "context.sqlite3"
    dictionary = (
        ContextDictionary.open_readonly(context_path).snapshot()
        if context_path.exists()
        else None
    )
    migration = WikiMigration(store, threads, dictionary)
    if preview is not None:
        if preview.resolve().is_relative_to(store.vault.resolve()):
            raise typer.BadParameter(
                "preview contains private metadata; save it outside the vault"
            )
        plan = migration.preview()
        preview.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        typer.echo(f"Preview saved: {preview}")
    else:
        assert apply is not None and backup is not None
        count = migration.apply(json.loads(apply.read_text()), backup, confirmed=yes)
        typer.echo(f"Migrated {count} files; backup: {backup}")
        typer.echo(
            json.dumps(migration.last_report.get("skipped", []), ensure_ascii=False)
        )


@cli.command()
def run(
    config: ConfigOption = None,
    llm: LlmOption = None,
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
    console: Annotated[
        bool | None,
        typer.Option(
            "--console/--no-console",
            help="Mirror logs to stderr (default: TTY outside supervised mode)",
        ),
    ] = None,
    interval: Annotated[
        float, typer.Option("--interval", min=0.5, help="Rescan interval seconds")
    ] = 5.0,
) -> None:
    """Run Weaver in the foreground until SIGTERM (launchd-friendly)."""
    settings = _load(config, init_directories=init_directories, llm=llm)
    if check_config or init_directories:
        typer.echo("Configuration valid.")
        return

    import logging

    from weaver.runtime import EXIT_ALREADY_RUNNING, AlreadyRunning, InstanceLock
    from weaver.runtime.logs import configure_logging

    log_file = configure_logging(settings.log_root, console=console)
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


@cli.command()
def reextract(
    config: ConfigOption = None,
    llm: LlmOption = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Apply a saved plan without LLM calls")
    ] = False,
    source: Annotated[
        str | None, typer.Option("--source", help="Plan only this source ID")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable report")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm apply/discard; required without a TTY"),
    ] = False,
    plan_out: Annotated[
        Path | None,
        typer.Option(
            "--plan-out",
            help="Save exact preview outside state_root (only preview write)",
        ),
    ] = None,
    plan: Annotated[
        Path | None, typer.Option("--plan", help="Previously reviewed plan to apply")
    ] = None,
    discard: Annotated[
        bool,
        typer.Option(
            "--discard-plan", help="Clear pending journal units; keeps committed facts"
        ),
    ] = False,
) -> None:
    """Preview saved approvals, or apply a fingerprinted plan with no model.

    API previews use memory accounting; CLI auth/cache and temporary protocol
    files retain their normal behavior. Apply requires --plan PATH.
    """
    import json
    import sqlite3
    from contextlib import nullcontext

    from weaver.llm import create_client
    from weaver.llm.policy_archive import configured_policy
    from weaver.loom.reextract import (
        apply_report,
        discard_plan,
        load_plan,
        render,
        save_plan,
        wiki_root,
    )
    from weaver.loom.reextract import reextract as execute
    from weaver.runtime import AlreadyRunning, InstanceLock

    settings = _load(config, llm=llm)
    mutation = apply or discard
    if mutation:
        import shlex

        root = shlex.quote(str(settings.state_root))
        typer.echo(
            f"적용 전 백업: cp -a {root} {shlex.quote(str(settings.state_root) + '.backup')} (데몬을 중지한 뒤 실행)",
            err=True,
        )
        if not yes:
            if not sys.stdin.isatty():
                typer.echo(
                    "Error: non-interactive apply/discard requires --yes", err=True
                )
                raise typer.Exit(2)
            if not typer.confirm("state_root 백업을 완료했으며 변경을 적용합니까?"):
                raise typer.Exit(2)
    else:
        typer.echo(
            "미리보기: Weaver 상태·사전·원장을 쓰지 않으며 --plan-out만 선택한 파일에 씁니다; LLM 과금 및 CLI 자체 인증/캐시·임시 파일 동작은 유지됩니다.",
            err=True,
        )
    try:
        with (
            InstanceLock(settings.state_root / "weaver.lock")
            if mutation
            else nullcontext()
        ):
            if discard:
                if apply or plan or plan_out:
                    raise ValueError(
                        "--discard-plan은 --apply/--plan/--plan-out과 함께 사용할 수 없습니다"
                    )
                count = discard_plan(settings.state_root, source)
                typer.echo(
                    json.dumps({"discarded": count})
                    if json_output
                    else f"대기 계획 {count}개 정리; 반영된 Thread는 유지됩니다. 다시 계획하십시오."
                )
                return
            if apply:
                if plan is None or plan_out is not None or source is not None:
                    raise ValueError(
                        "--apply --plan PATH 필요 (--source/--plan-out 제외)"
                    )
                report = apply_report(settings.state_root, load_plan(plan))
            else:
                if plan is not None:
                    raise ValueError("--plan에는 --apply가 필요합니다")
                report = execute(
                    settings.state_root,
                    wiki_root(settings),
                    create_client(
                        settings.llm,
                        state_root=settings.state_root,
                        dry_run=True,
                        policy=configured_policy(settings, snapshot=True),
                    ),
                    source=source,
                    timezone=settings.time.timezone,
                )
                if plan_out:
                    save_plan(plan_out, settings.state_root, report)
    except AlreadyRunning:
        typer.echo(
            "Error: resident daemon holds weaver.lock; stop it before apply/discard",
            err=True,
        )
        raise typer.Exit(3) from None
    except (ValueError, OSError, sqlite3.Error) as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        typer.echo(
            json.dumps({"error": message}, ensure_ascii=False)
            if json_output
            else f"Error: {message}",
            err=not json_output,
        )
        raise typer.Exit(2) from None
    typer.echo(report.model_dump_json() if json_output else render(report))
    if report.errors or any(item.error for item in report.sources):
        raise typer.Exit(1)


@cli.command("install-agent")
def install_agent(
    config: ConfigOption = None,
    llm: LlmOption = None,
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

    settings = _load(config, llm=llm)
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
        llm_backend=llm.value if llm is not None else None,
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
def status(config: ConfigOption = None, llm: LlmOption = None) -> None:
    """Show launchd state and the running instance's last status report."""
    from weaver.runtime import launchd

    settings = _load(config, llm=llm)
    typer.echo(f"launchd: {launchd.status(launchctl=launchd.run_launchctl)}")
    report = settings.state_root / "runtime-status.json"
    try:
        typer.echo(report.read_text(encoding="utf-8"), nl=False)
    except FileNotFoundError:
        typer.echo("runtime: no status report yet")
    typer.echo(f"\nlog: {settings.log_root / 'weaver.log'}")


@cli.command()
def web(
    config: ConfigOption = None,
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    host: str = "127.0.0.1",
    static_dir: Annotated[Path | None, typer.Option(envvar="WEAVER_WEB_DIR")] = None,
) -> None:
    """Serve the local web UI on loopback only."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise typer.BadParameter("host must be loopback (127.0.0.1, ::1, localhost)")
    import os

    os.umask(0o077)
    import uvicorn

    from weaver.web import create_app

    settings = _load(config)
    if static_dir is None:
        candidate = Path(__file__).resolve().parents[3] / "web" / "out"
        if candidate.is_dir():
            static_dir = candidate
    if static_dir is None:
        typer.echo(
            "Warning: web/out missing; API only. Run pnpm --dir web build.", err=True
        )
    elif not static_dir.is_dir():
        raise typer.BadParameter("static-dir must be an existing directory")
    uvicorn.run(create_app(settings, static_dir), host=host, port=port)


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
