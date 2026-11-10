"""Typer entry point for the offline event summary evaluation."""

import json
from pathlib import Path

import typer

from .client import ReplayClient
from .summary_runner import SUMMARY_CASES, evaluate_summary

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Evaluate Weaver prompts offline."""


@app.command("summary")
def summary_command(
    cases: Path = SUMMARY_CASES,
    output: Path = Path("reports/summary-evals.json"),
) -> None:
    """Replay summary evaluation fixtures without accessing external services."""
    try:
        dataset = json.loads(cases.read_text(encoding="utf-8"))
        if dataset["version"] != 1 or not dataset["cases"]:
            raise ValueError("unsupported or empty summary dataset")
        rows = [
            evaluate_summary(case, ReplayClient(case["responses"]))
            for case in dataset["cases"]
        ]
        report = {"version": 1, "cases": rows}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        typer.echo(f"Summary evaluation: {len(rows)} cases")
        if any(not all(row["metrics"].values()) for row in rows):
            raise typer.Exit(1)
    except (OSError, ValueError, KeyError, TypeError) as error:
        typer.echo(f"Evaluation error: {error}", err=True)
        raise typer.Exit(2) from None


if __name__ == "__main__":
    app()
