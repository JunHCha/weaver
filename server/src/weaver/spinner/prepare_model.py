"""Explicit network-enabled preparation, separate from offline transcription.

Run ``uv run --project server python -m weaver.spinner.prepare_model --help``.
This command downloads weights only when explicitly invoked. See MODEL_REVISION
and the committed uv.lock for the reproducible model/runtime versions.
"""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer

from weaver.config import ConfigError, load_config
from weaver.spinner.stt import (
    MODEL_REPO,
    MODEL_REVISION,
    SttEngineError,
    import_stt_dependency,
)

WEIGHTS_SHA256 = "951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6"

cli = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


def prepare_model(destination: Path) -> None:
    """Download the selected revision, verify weights, publish without overwrite.

    An existing destination is rejected. This command does not import MLX,
    load a model, or run inference; only the Hub client is loaded lazily.
    """
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("Model destination already exists")
    hub = import_stt_dependency("huggingface_hub")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".weaver-model-", dir=destination.parent) as temp:
        staged = Path(temp) / "model"
        hub.snapshot_download(
            MODEL_REPO,
            revision=MODEL_REVISION,
            allow_patterns=["config.json", "weights.safetensors"],
            local_dir=str(staged),
        )
        with (staged / "weights.safetensors").open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != WEIGHTS_SHA256:
            raise ValueError("Downloaded model checksum mismatch")
        if not (staged / "config.json").is_file():
            raise ValueError("Downloaded model config is missing")
        # The revision is also visible without loading the engine or its weights.
        (staged / "weaver-revision.txt").write_text(MODEL_REVISION + "\n")
        staged.rename(destination)


@cli.command()
def prepare(
    config: Annotated[Path | None, typer.Option(help="Weaver TOML file")] = None,
) -> None:
    """Download the pinned FP16 model to settings.stt.model_path (network needed)."""
    try:
        settings = load_config(config)
        prepare_model(settings.stt.model_path)
    except SttEngineError as error:
        typer.echo(f"Model preparation failed: {error}", err=True)
        raise typer.Exit(1) from None
    except (ConfigError, OSError, ValueError, ImportError) as error:
        typer.echo(f"Model preparation failed: {type(error).__name__}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Prepared {MODEL_REPO}@{MODEL_REVISION}")


if __name__ == "__main__":
    cli()
