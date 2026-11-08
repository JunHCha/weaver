"""Pinned preparation with a fake Hub client; no actual downloads."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from weaver.spinner import prepare_model as module
from weaver.spinner.stt import MODEL_REPO, MODEL_REVISION


@pytest.mark.parametrize("valid", [True, False])
def test_prepare_revision_hash_and_cleanup(tmp_path, monkeypatch, valid):
    weights = b"fake weights"
    monkeypatch.setattr(module, "WEIGHTS_SHA256", hashlib.sha256(weights).hexdigest())
    calls = []

    def download(repo, *, revision, allow_patterns, local_dir):
        calls.append((repo, revision, allow_patterns))
        target = Path(local_dir)
        target.mkdir()
        (target / "weights.safetensors").write_bytes(weights if valid else b"corrupt")
        (target / "config.json").write_text("{}")

    monkeypatch.setattr(
        "weaver.spinner.stt.importlib.import_module",
        lambda _: SimpleNamespace(snapshot_download=download),
    )
    destination = tmp_path / "model"
    if valid:
        module.prepare_model(destination)
        assert (
            destination / "weaver-revision.txt"
        ).read_text().strip() == MODEL_REVISION
        with pytest.raises(FileExistsError):
            module.prepare_model(destination)
    else:
        with pytest.raises(ValueError, match="checksum"):
            module.prepare_model(destination)
        assert not destination.exists()
    assert calls == [
        (MODEL_REPO, MODEL_REVISION, ["config.json", "weights.safetensors"])
    ]
    assert not list(tmp_path.glob(".weaver-model-*"))


def test_missing_dependency_has_install_guidance_without_creating_files(
    tmp_path, monkeypatch
):
    from weaver.spinner.stt import SttEngineError

    def missing(name):
        raise ModuleNotFoundError(name, name="hub_transitive_dependency")

    monkeypatch.setattr("weaver.spinner.stt.importlib.import_module", missing)
    destination = tmp_path / "not-created" / "model"
    with pytest.raises(
        SttEngineError, match="uv sync --project server --locked --extra stt"
    ):
        module.prepare_model(destination)
    assert not destination.parent.exists()


def test_cli_missing_dependency_shows_install_guidance(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    def missing(name):
        raise ModuleNotFoundError(name, name="hub_transitive_dependency")

    monkeypatch.setattr("weaver.spinner.stt.importlib.import_module", missing)
    monkeypatch.setattr(
        module,
        "load_config",
        lambda _: SimpleNamespace(stt=SimpleNamespace(model_path=tmp_path / "model")),
    )
    result = CliRunner().invoke(module.cli)
    assert result.exit_code == 1
    assert "uv sync --project server --locked --extra stt" in result.output
    assert "hub_transitive_dependency" in result.output
