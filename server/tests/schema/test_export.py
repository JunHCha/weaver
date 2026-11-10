"""Exported JSON Schemas are deterministic and match the committed files."""

import json
from pathlib import Path

from weaver.schema import SHARED_MODELS
from weaver.schema.export import export

ROOT = Path(__file__).resolve().parents[3]


def check_refs(value, root):
    if isinstance(value, dict):
        ref = value.get("$ref")
        if ref is not None:
            assert ref.startswith("#/$defs/")
            assert ref.removeprefix("#/").split("/")[1] in root["$defs"]
        for item in value.values():
            check_refs(item, root)
    elif isinstance(value, list):
        for item in value:
            check_refs(item, root)


def test_export_matches_committed_artifacts_and_is_reproducible(tmp_path):
    export(tmp_path)
    first = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    export(tmp_path)
    assert first == {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    assert len(first) == len(SHARED_MODELS)
    committed = {p.name: p.read_bytes() for p in (ROOT / "schema").glob("*.json")}
    assert first == committed
    for name, content in first.items():
        schema = json.loads(content)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"] == f"urn:weaver:schema:{name.removesuffix('.schema.json')}"
        check_refs(schema, schema)


def test_no_version_directory_or_stale_schema_remains(tmp_path):
    export(tmp_path)
    assert not (tmp_path / "v2").exists()
    assert not (ROOT / "schema" / "v2").exists()
    for stale in ("Entity", "Relation", "Proposal"):
        assert not (ROOT / "schema" / f"{stale}.schema.json").exists()


def test_every_example_uses_its_public_model():
    by_name = {m.__name__: m for m in SHARED_MODELS}
    for path in (ROOT / "schema" / "examples").glob("*.json"):
        by_name[path.stem.split("-")[0]].model_validate_json(path.read_text())


def test_server_only_models_are_not_exported(tmp_path):
    export(tmp_path)
    text = "".join(p.read_text() for p in tmp_path.glob("*.json"))
    assert "ArchivedSource" not in text and "payload_path" not in text
