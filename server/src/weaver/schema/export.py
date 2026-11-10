"""Deterministic standalone Draft 2020-12 schemas in validation mode.

Run from the repository root:
    uv run --project server python -m weaver.schema.export --output schema
"""

import json
from pathlib import Path
from typing import Annotated

import typer

from .models import SHARED_MODELS


def export(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for model in SHARED_MODELS:
        schema = model.model_json_schema(mode="validation", by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"urn:weaver:schema:{model.__name__}"
        (output / f"{model.__name__}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main(output: Annotated[Path, typer.Option()] = Path("schema")) -> None:
    """Write one JSON Schema file per shared model."""
    export(output)


if __name__ == "__main__":
    typer.run(main)
