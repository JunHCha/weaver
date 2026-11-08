"""Typer entry point: validate, run, and compare Weaver evaluations."""

import json
import math
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer

from weaver.config import load_config
from weaver.llm import AnthropicClient
from weaver.llm.ledger import Ledger
from weaver.llm.types import LlmClient

from .dataset import CASES_DIR, DatasetError, LoadedCase, load_dataset, summarize
from .runner import (
    FIXTURES,
    ReplayClient,
    RunLedger,
    baseline_snapshot,
    compare,
    digest,
    run,
    summary,
)

app = typer.Typer(invoke_without_command=True, no_args_is_help=False)


@app.callback()
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(json.dumps(summarize(load_dataset()), ensure_ascii=False, indent=2))


@app.command("validate")
def validate(cases: Path = CASES_DIR) -> None:
    typer.echo(json.dumps(summarize(load_dataset(cases)), ensure_ascii=False, indent=2))


@app.command("run")
def run_command(
    cases: Path = CASES_DIR,
    output: Path = Path("reports/evals.json"),
    fixtures: Path = FIXTURES,
    baseline: Path | None = None,
    save_baseline: Path | None = None,
    tolerance: Annotated[float, typer.Option(min=0, max=1)] = 0,
    live: bool = False,
    max_cost_usd: float | None = None,
    confirm_budget: bool = False,
    config: Path | None = None,
) -> None:
    """Default: replay fake responses; --live also requires an explicit budget."""
    try:
        if not math.isfinite(tolerance):
            raise ValueError("tolerance must be finite")
        loaded = load_dataset(cases)
        # Preflight before creating a client, state directories or paid calls.
        old: dict[str, Any] | None = (
            json.loads(baseline.read_text()) if baseline else None
        )
        if old is not None and (
            not isinstance(old, dict)
            or old.get("report_version") != 1
            or old.get("scoring_version") != "v1"
            or old.get("dataset_hash")
            != digest([c.case.model_dump(mode="json", by_alias=True) for c in loaded])
        ):
            raise ValueError("incompatible baseline")
        ledger: RunLedger | None = None
        fixture_hash = None
        if live:
            if (
                max_cost_usd is None
                or not math.isfinite(max_cost_usd)
                or max_cost_usd <= 0
                or not confirm_budget
            ):
                raise ValueError(
                    "--live requires positive --max-cost-usd and --confirm-budget"
                )
            settings = load_config(config)
            shared = Ledger(settings.state_root / "llm.sqlite3")
            ledger = RunLedger(
                shared,
                Ledger(
                    settings.state_root / f"eval-{uuid4().hex}.sqlite3",
                    warn_usd=max_cost_usd,
                    stop_usd=max_cost_usd,
                ),
            )
            client = AnthropicClient(settings.llm, ledger)

            def factory(case: LoadedCase) -> LlmClient:
                return client
        else:
            if max_cost_usd is not None or confirm_budget:
                raise ValueError("budget options require --live")
            responses = json.loads(fixtures.read_text(encoding="utf-8"))
            if (
                not isinstance(responses, dict)
                or responses.get("version") != 1
                or not isinstance(responses.get("cases"), dict)
            ):
                raise ValueError("unsupported fixture version")
            for case in loaded:
                if case.case.case_id not in responses["cases"]:
                    raise ValueError(f"missing fixture: {case.case.case_id}")
            fixture_hash = digest(responses)

            def factory(case: LoadedCase) -> LlmClient:
                return ReplayClient(responses["cases"][case.case.case_id])

        report = run(
            loaded,
            factory,
            backend="anthropic" if live else "fixture",
            fixture_hash=fixture_hash,
        )
        if ledger:
            report["ledger"] = ledger.records()
            report["budget_usd"] = max_cost_usd
            costs = [r["cost_usd"] for r in report["ledger"] if r.get("attempts")]
            report["cost_usd"] = (
                sum(costs) if all(c is not None for c in costs) else None
            )
        else:
            report["cost_usd"] = 0
        if old is not None:
            report["comparison"] = compare(report, old, tolerance)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        output.with_suffix(".txt").write_text(summary(report) + "\n", encoding="utf-8")
        if save_baseline:
            save_baseline.parent.mkdir(parents=True, exist_ok=True)
            save_baseline.write_text(
                json.dumps(baseline_snapshot(report), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
        typer.echo(summary(report))
        failed = any(row["errors"] for row in report["cases"])
        if failed or report.get("comparison", {}).get("regressed"):
            raise typer.Exit(1)
    except (DatasetError, OSError, ValueError, KeyError) as error:
        typer.echo(f"Evaluation error: {error}", err=True)
        raise typer.Exit(2) from None


if __name__ == "__main__":
    app()
