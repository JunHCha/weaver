"""Deterministic standalone Draft 2020-12 schemas in validation mode.

Run from the repository root:
    uv run --project server python -m weaver.schema.export --output schema
"""

import argparse
import json
from pathlib import Path

from . import SHARED_MODELS, v2

V2_MODELS = (
    v2.Thread,
    v2.ThreadDraft,
    v2.EventSummary,
    v2.ReviewPackage,
    v2.Question,
    v2.Verdict,
)


def export(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for version, models in ((1, SHARED_MODELS), (2, V2_MODELS)):
        destination = output if version == 1 else output / "v2"
        destination.mkdir(parents=True, exist_ok=True)
        for model in models:
            schema = model.model_json_schema(mode="validation", by_alias=True)
            schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            schema["$id"] = f"urn:weaver:schema:v{version}:{model.__name__}"
            (destination / f"{model.__name__}.schema.json").write_text(
                json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("schema"))
    export(parser.parse_args().output)


if __name__ == "__main__":
    main()
