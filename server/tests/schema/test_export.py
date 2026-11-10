"""Check artifact freshness, deterministic bytes and every document-local $ref."""

import json
from pathlib import Path

from weaver.schema import SHARED_MODELS
from weaver.schema.export import export

from .test_contract import ROOT


def test_export_matches_committed_artifacts_and_is_reproducible(tmp_path):
    export(tmp_path)
    first = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    export(tmp_path)
    assert first == {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    assert len(first) == len(SHARED_MODELS) == 10
    for name, content in first.items():
        assert content == (ROOT / "schema" / name).read_bytes()
        schema = json.loads(content)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
        assert "schema_version" in schema["required"]
        check_refs(schema, schema)


def check_refs(value, root):
    if isinstance(value, dict):
        if "$ref" in value:
            assert value["$ref"].startswith("#/"), value["$ref"]
            target = root
            for token in value["$ref"][2:].split("/"):
                target = target[token.replace("~1", "/").replace("~0", "~")]
            assert isinstance(target, dict)
        for child in value.values():
            check_refs(child, root)
    elif isinstance(value, list):
        for child in value:
            check_refs(child, root)


def test_every_minimum_example_uses_its_public_model():
    by_name = {m.__name__: m for m in SHARED_MODELS}
    for path in (ROOT / "schema" / "examples").glob("*.json"):
        by_name[path.stem.split("-")[0]].model_validate_json(path.read_text())


def test_internal_models_are_not_exported(tmp_path: Path):
    export(tmp_path)
    text = "\n".join(p.read_text() for p in tmp_path.glob("*.json"))
    for internal in [
        "EntityState",
        "SetState",
        "StateValue",
        "ArchivedSource",
        "payload_path",
        "Candidate",
        "Extraction",
        "EntityIndex",
    ]:
        assert internal not in text


def test_v2_export_matches_committed_artifacts(tmp_path):
    from weaver.schema.export import V2_MODELS

    export(tmp_path)
    generated = {p.name: p.read_bytes() for p in (tmp_path / "v2").glob("*.json")}
    committed = {
        p.name: p.read_bytes() for p in (ROOT / "schema" / "v2").glob("*.json")
    }
    assert len(generated) == len(V2_MODELS)
    assert generated == committed
    for content in generated.values():
        schema = json.loads(content)
        assert schema["$id"].startswith("urn:weaver:schema:v2:")
        check_refs(schema, schema)
