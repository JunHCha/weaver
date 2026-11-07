"""Local STT boundary. Importing this module never loads a speech model.

Model preparation is explicit and separate from transcription. The production
engine receives only an absolute existing model directory, never a Hub repo ID.
"""

import importlib
import math
import subprocess
import sys
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, TypeGuard, cast

MODEL_REPO = "mlx-community/whisper-large-v3-turbo"
MODEL_REVISION = "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
ENGINE_VERSION = "0.4.3"


@dataclass(frozen=True)
class ModelMetadata:
    """Selected preparation identity, kept outside the Transcript contract."""

    model_path: Path
    engine: str = "mlx-whisper"
    engine_version: str = ENGINE_VERSION
    model_repo: str = MODEL_REPO
    model_revision: str = MODEL_REVISION
    precision: str = "float16"


class SttError(Exception):
    """Base class for transcription failures (never a silent transcript)."""


class MissingAudioError(SttError):
    """The input file does not exist or is not a regular file."""


class AudioDecodeError(SttError):
    """Local decoding failed, including corrupt or unsupported audio."""


class ModelNotFoundError(SttError):
    """The prepared local model files are missing."""


class SttEngineError(SttError):
    """Engine import, model loading, or inference failed."""


class InvalidEngineOutputError(SttEngineError):
    """The engine returned an invalid segment structure."""


@dataclass(frozen=True)
class Segment:
    segment_id: str
    start: float
    end: float
    text: str
    speaker: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.segment_id, str)
            or not self.segment_id
            or not _time(self.start)
            or not _time(self.end)
            or self.start > self.end
            or not isinstance(self.text, str)
            or (self.speaker is not None and not isinstance(self.speaker, str))
        ):
            raise InvalidEngineOutputError("Invalid transcript segment")


@dataclass(frozen=True)
class Transcript:
    """Immutable, ordered utterances; overlapping intervals are allowed.

    Segment IDs are scoped to this transcript. Engine tokens, scores and model
    identity are deliberately outside this shared text contract.
    """

    segments: tuple[Segment, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.segments, tuple) or not all(
            isinstance(s, Segment) for s in self.segments
        ):
            raise InvalidEngineOutputError("Expected immutable transcript segments")
        if len({s.segment_id for s in self.segments}) != len(self.segments) or any(
            a.start > b.start
            for a, b in zip(self.segments, self.segments[1:], strict=False)
        ):
            raise InvalidEngineOutputError("Segments must be ordered with unique IDs")


class Stt(Protocol):
    def transcribe(self, audio: Path, language: str = "ko") -> Transcript: ...


class Engine(Protocol):
    """Injectable engine seam for tests, using 16 kHz mono float PCM."""

    def __call__(
        self, samples: Sequence[float], model_path: Path, language: str
    ) -> object: ...


def _time(value: object) -> TypeGuard[int | float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _normalize(output: object) -> Transcript:
    if not isinstance(output, dict) or not isinstance(output.get("segments"), list):
        raise InvalidEngineOutputError("Expected engine segments list")
    segments: list[Segment] = []
    for raw in output["segments"]:
        if not isinstance(raw, dict):
            raise InvalidEngineOutputError("Expected engine segment object")
        start, end, text = raw.get("start"), raw.get("end"), raw.get("text")
        if not _time(start) or not _time(end) or not isinstance(text, str):
            raise InvalidEngineOutputError("Invalid engine segment fields")
        segments.append(Segment("pending", float(start), float(end), text))
    # Stable sort preserves engine order at equal start times, including overlap.
    segments.sort(key=lambda segment: segment.start)
    return Transcript(
        tuple(
            Segment(f"seg_{index:06d}", s.start, s.end, s.text)
            for index, s in enumerate(segments)
        )
    )


def _mlx_engine(samples: Sequence[float], model_path: Path, language: str) -> object:
    # Both MLX and numpy are imported lazily; Linux CI uses the fake seam.
    mlx_whisper = importlib.import_module("mlx_whisper")
    numpy = importlib.import_module("numpy")
    return cast(
        object,
        mlx_whisper.transcribe(
            numpy.asarray(samples, dtype=numpy.float32),
            path_or_hf_repo=str(model_path),
            language=language,
            task="transcribe",
            temperature=0.0,
            condition_on_previous_text=False,
            verbose=None,
            fp16=True,
        ),
    )


class MlxStt:
    """MLX large-v3-turbo FP16 adapter; no diarization or VAD in the MVP.

    ``ffmpeg`` must be installed locally. It writes temporary 16 kHz mono PCM;
    original audio is read only. Exact digital silence bypasses inference, but
    near-silence/noise can still hallucinate and requires future VAD evaluation.
    """

    def __init__(self, model_path: Path, *, engine: Engine = _mlx_engine) -> None:
        self.model_path = model_path.resolve()
        self.metadata = ModelMetadata(self.model_path)
        self._engine = engine

    def transcribe(self, audio: Path, language: str = "ko") -> Transcript:
        if not audio.is_file():
            raise MissingAudioError("Audio file is missing")
        if (
            not (self.model_path / "config.json").is_file()
            or not (self.model_path / "weights.safetensors").is_file()
        ):
            raise ModelNotFoundError("Prepare the local STT model before transcription")
        with TemporaryDirectory(prefix="weaver-stt-") as temporary:
            pcm = Path(temporary) / "audio.f32"
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        str(audio.resolve()),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-f",
                        "f32le",
                        str(pcm),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                samples = array("f")
                samples.frombytes(pcm.read_bytes())
                if sys.byteorder != "little":
                    samples.byteswap()
                if not samples or not all(math.isfinite(s) for s in samples):
                    raise ValueError("Empty or nonfinite PCM")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise AudioDecodeError("Cannot decode local audio") from error
            if not any(samples):
                return Transcript()
            try:
                output = self._engine(samples, self.model_path, language)
            except Exception as error:
                raise SttEngineError("Local STT engine failed") from error
            return _normalize(output)
