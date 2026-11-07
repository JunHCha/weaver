"""Explicit paid smoke, never collected by pytest. Synthetic input only.

uv run --project server python server/evals/llm_smoke.py --config /path/config.toml
Requires llm.backend=anthropic and a configured llm.api_key_env reference.
Makes two paid requests through the normal monthly reservation ledger.
"""

from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from weaver.config import load_config
from weaver.llm import create_client


class SmokeAnswer(BaseModel):
    count: int


def main(config: Annotated[Path, typer.Option()]) -> None:
    settings = load_config(config)
    client = create_client(settings.llm, state_root=settings.state_root)
    text = client.generate_text("smoke.text", "Reply with a single word.", "Say hello.")
    answer = client.generate_json(
        "smoke.json",
        "Return the requested object.",
        "Return count equal to 1.",
        SmokeAnswer,
    )
    if not text or answer.count != 1:
        raise RuntimeError("smoke validation failed")
    typer.echo(
        "Anthropic text + structured JSON smoke passed; see state_root/llm.sqlite3"
    )


if __name__ == "__main__":
    typer.run(main)
