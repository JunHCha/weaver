"""Deterministic standalone Draft 2020-12 schemas in validation mode.

Run from the repository root:
    uv run --project server python -m weaver.schema.export --output schema
"""

import argparse
import json
from pathlib import Path

from . import SHARED_MODELS


def export(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for model in SHARED_MODELS:
        schema = model.model_json_schema(mode="validation", by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"urn:weaver:schema:v1:{model.__name__}"
        (output / f"{model.__name__}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("schema"))
    export(parser.parse_args().output)


if __name__ == "__main__":
    main()
