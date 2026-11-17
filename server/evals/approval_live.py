"""Explicit paid synthetic eval; never imported or run by pytest.

Only the LLM settings are read from config; no configured vault or state is used.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer

from weaver.config import load_config
from weaver.evals.approval_runner import (
    APPROVAL_CASES,
    evaluate_approval,
    evaluate_bootstrap,
)
from weaver.llm import create_client


def main(
    config: Annotated[Path, typer.Option()],
    output: Path = Path("reports/approval-live.json"),
) -> None:
    settings = load_config(config)
    dataset = json.loads(APPROVAL_CASES.read_text())
    with TemporaryDirectory(prefix="weaver-live-eval-") as directory:
        client = create_client(settings.llm, state_root=Path(directory))
        rows = [evaluate_approval(case, client) for case in dataset["cases"]]
        rows.append(evaluate_bootstrap(dataset["bootstrap"], client))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"version": 1, "cases": rows}, ensure_ascii=False, indent=2) + "\n"
    )
    if any(not all(row["metrics"].values()) for row in rows):
        raise typer.Exit(1)
    typer.echo(f"Synthetic live evaluation: {len(rows)} cases passed")


if __name__ == "__main__":
    typer.run(main)
