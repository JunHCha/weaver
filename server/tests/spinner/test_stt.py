"""No model imports, downloads, inference, or private audio."""

import struct
import subprocess
from pathlib import Path

import pytest

from weaver.spinner.stt import (
    AudioDecodeError,
    InvalidEngineOutputError,
    MissingAudioError,
    MlxStt,
    ModelNotFoundError,
    Stt,
    SttEngineError,
)


@pytest.fixture
def model(tmp_path: Path) -> Path:
    path = tmp_path / "model"
    path.mkdir()
    (path / "config.json").write_text("{}")
    (path / "weights.safetensors").write_bytes(b"fake")
    return path


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "한국어.m4a"
    path.write_bytes(b"original audio")
    return path


@pytest.fixture
def decoder(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    converted: list[Path] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        target = Path(command[-1])
        converted.append(target)
        target.write_bytes(struct.pack("<ff", 0.25, -0.25))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("weaver.spinner.stt.subprocess.run", run)
    return converted


def test_korean_mapping_order_overlap_ids_and_cleanup(model, audio, decoder):
    def engine(samples, model_path, language):
        assert list(samples) == [0.25, -0.25]
        assert model_path == model.resolve()
        assert language == "ko"
        return {
            "segments": [
                {"start": 1, "end": 3, "text": "내일 만나요", "speaker": "fake"},
                {"start": 0, "end": 2, "text": "안녕하세요"},
            ]
        }

    stt: Stt = MlxStt(model, engine=engine)
    result = stt.transcribe(audio)
    assert [s.text for s in result.segments] == ["안녕하세요", "내일 만나요"]
    assert [s.segment_id for s in result.segments] == ["seg_000000", "seg_000001"]
    assert all(s.speaker is None for s in result.segments)
    assert result == stt.transcribe(audio)
    assert audio.read_bytes() == b"original audio"
    assert all(not p.parent.exists() for p in decoder)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"segments": None},
        {"segments": [None]},
        {"segments": [{"start": 0, "end": 1}]},
        *(
            {"segments": [{"start": start, "end": end, "text": text}]}
            for start, end, text in [
                (-1, 1, "x"),
                (2, 1, "x"),
                (float("nan"), 1, "x"),
                (0, float("inf"), "x"),
                (True, 1, "x"),
                ("0", 1, "x"),
                (0, 1, None),
            ]
        ),
    ],
)
def test_rejects_malformed_engine_output(model, audio, decoder, bad):
    with pytest.raises(InvalidEngineOutputError):
        MlxStt(model, engine=lambda *_: bad).transcribe(audio)
    assert not decoder[0].parent.exists()


def test_missing_audio(model, tmp_path):
    with pytest.raises(MissingAudioError):
        MlxStt(model, engine=lambda *_: pytest.fail("engine called")).transcribe(
            tmp_path / "absent.wav"
        )


def test_missing_model(audio, tmp_path, decoder):
    with pytest.raises(ModelNotFoundError):
        MlxStt(
            tmp_path / "absent", engine=lambda *_: pytest.fail("engine called")
        ).transcribe(audio)
    assert decoder == []


@pytest.mark.parametrize("failure", ["corrupt input", "unsupported codec"])
def test_decode_failures_are_not_engine_failures(model, audio, monkeypatch, failure):
    paths = []

    def fail(command, **kwargs):
        paths.append(Path(command[-1]))
        raise subprocess.CalledProcessError(1, command, stderr=failure.encode())

    monkeypatch.setattr("weaver.spinner.stt.subprocess.run", fail)
    with pytest.raises(AudioDecodeError):
        MlxStt(model, engine=lambda *_: pytest.fail("engine called")).transcribe(audio)
    assert not paths[0].parent.exists()
    assert audio.read_bytes() == b"original audio"


def test_engine_failure_cleanup(model, audio, decoder):
    def fail(*_):
        raise RuntimeError("engine failed")

    with pytest.raises(SttEngineError):
        MlxStt(model, engine=fail).transcribe(audio)
    assert not decoder[0].parent.exists()


def test_digital_silence_skips_engine(model, audio, monkeypatch):
    def run(command, **kwargs):
        Path(command[-1]).write_bytes(bytes(40))

    monkeypatch.setattr("weaver.spinner.stt.subprocess.run", run)
    result = MlxStt(model, engine=lambda *_: pytest.fail("engine called")).transcribe(
        audio
    )
    assert result.segments == ()


@pytest.mark.parametrize("data", [b"", b"x", struct.pack("<f", float("nan"))])
def test_invalid_decoded_pcm(model, audio, monkeypatch, data):
    def run(command, **kwargs):
        Path(command[-1]).write_bytes(data)

    monkeypatch.setattr("weaver.spinner.stt.subprocess.run", run)
    with pytest.raises(AudioDecodeError):
        MlxStt(model, engine=lambda *_: pytest.fail("engine called")).transcribe(audio)


def test_mlx_call_uses_local_array_and_fixed_options(model, monkeypatch):
    from types import SimpleNamespace

    from weaver.spinner.stt import _mlx_engine

    calls = []

    def transcribe(samples, **kwargs):
        calls.append((samples, kwargs))
        return {"segments": []}

    modules = {
        "mlx_whisper": SimpleNamespace(transcribe=transcribe),
        "numpy": SimpleNamespace(asarray=lambda samples, dtype: samples, float32="f32"),
    }
    monkeypatch.setattr(
        "weaver.spinner.stt.importlib.import_module", modules.__getitem__
    )
    assert _mlx_engine([0.5], model, "ko") == {"segments": []}
    assert calls == [
        (
            [0.5],
            {
                "path_or_hf_repo": str(model),
                "language": "ko",
                "task": "transcribe",
                "temperature": 0.0,
                "condition_on_previous_text": False,
                "verbose": None,
                "fp16": True,
            },
        )
    ]


def test_real_local_decode_without_model(tmp_path, model):
    import shutil
    import wave

    if shutil.which("ffmpeg") is None:
        pytest.skip("optional ffmpeg decode-only check; fake decode tests always run")
    audio = tmp_path / "stereo.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((2, 2, 8000, 0, "NONE", "PCM"))
        stream.writeframes(struct.pack("<hh", 1000, 2000) * 8000)
    original = audio.read_bytes()

    def engine(samples, model_path, language):
        assert len(samples) == 16000
        # ffmpeg's default stereo downmix uses equal-power coefficients.
        assert samples[8000] == pytest.approx(3000 / (2**0.5 * 32768), rel=0.001)
        return {"segments": [{"start": 0, "end": 1, "text": "한국어 가짜 결과"}]}

    assert (
        MlxStt(model, engine=engine).transcribe(audio).segments[0].text
        == "한국어 가짜 결과"
    )
    assert audio.read_bytes() == original
    audio.write_bytes(b"corrupt container")
    with pytest.raises(AudioDecodeError):
        MlxStt(model, engine=engine).transcribe(audio)


def test_empty_engine_result_and_metadata(model, audio, decoder):
    stt = MlxStt(model, engine=lambda *_: {"segments": []})
    assert stt.transcribe(audio).segments == ()
    assert stt.metadata.model_revision == "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
    assert stt.metadata.precision == "float16"


def test_transcript_is_immutable_and_validated():
    from dataclasses import FrozenInstanceError

    from weaver.spinner.stt import Segment, Transcript

    first = Segment("a", 0, 2, "겹침")
    second = Segment("b", 1, 3, "허용")
    transcript = Transcript((first, second))
    with pytest.raises(FrozenInstanceError):
        transcript.segments = ()
    with pytest.raises(FrozenInstanceError):
        first.text = "changed"
    for segments in [(second, first), (first, first), [first]]:
        with pytest.raises(InvalidEngineOutputError):
            Transcript(segments)


def test_equal_start_order_and_custom_language(model, audio, decoder):
    def engine(samples, model_path, language):
        assert language == "en"
        return {
            "segments": [
                {"start": 0, "end": 1, "text": text} for text in ["first", "second"]
            ]
        }

    result = MlxStt(model, engine=engine).transcribe(audio, "en")
    assert [s.text for s in result.segments] == ["first", "second"]


def test_unrepresentable_timestamp_is_invalid_output(model, audio, decoder):
    with pytest.raises(InvalidEngineOutputError):
        MlxStt(
            model,
            engine=lambda *_: {
                "segments": [{"start": 10**1000, "end": 10**1001, "text": "invalid"}]
            },
        ).transcribe(audio)


def test_decoder_has_deadline_and_restricts_formats(model, audio, monkeypatch):
    def fail(command, **kwargs):
        assert kwargs["timeout"] == 120.0
        assert command[command.index("-protocol_whitelist") + 1] == "file"
        assert "concat" not in command[command.index("-format_whitelist") + 1].split(
            ","
        )
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("weaver.spinner.stt.subprocess.run", fail)
    with pytest.raises(AudioDecodeError) as error:
        MlxStt(model, engine=lambda *_: pytest.fail("engine called")).transcribe(audio)
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)


def test_fifo_input_is_rejected_before_decoder(model, tmp_path, monkeypatch):
    import os

    fifo = tmp_path / "fifo.wav"
    os.mkfifo(fifo)
    monkeypatch.setattr(
        "weaver.spinner.stt.subprocess.run",
        lambda *_a, **_k: pytest.fail("decoder called"),
    )
    with pytest.raises(MissingAudioError):
        MlxStt(model, engine=lambda *_: pytest.fail("engine called")).transcribe(fifo)


def test_timeout_kills_decoder_and_cleans_partial_output(
    model, audio, tmp_path, monkeypatch
):
    import os
    import sys

    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    pid_file = tmp_path / "decoder.pid"
    converted = []
    real_run = subprocess.run

    def blocked_decoder(command, **kwargs):
        converted.append(Path(command[-1]))
        # A real child blocks on a writerless FIFO after creating partial output.
        script = (
            "import os,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); "
            "Path(sys.argv[2]).write_bytes(b'partial'); "
            "os.open(sys.argv[3], os.O_RDONLY)"
        )
        return real_run(
            [sys.executable, "-c", script, str(pid_file), command[-1], str(fifo)],
            **kwargs,
        )

    monkeypatch.setattr("weaver.spinner.stt.subprocess.run", blocked_decoder)
    with pytest.raises(AudioDecodeError) as error:
        MlxStt(
            model, engine=lambda *_: pytest.fail("engine called"), decode_timeout=0.5
        ).transcribe(audio)
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    assert not converted[0].parent.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert audio.read_bytes() == b"original audio"


def test_ffconcat_cannot_open_referenced_fifo(model, tmp_path, monkeypatch):
    import os
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("optional real ffmpeg format restriction check")
    os.mkfifo(tmp_path / "referenced.wav")
    audio = tmp_path / "audio.ffconcat"
    audio.write_text("ffconcat version 1.0\nfile 'referenced.wav'\n")
    converted = []
    real_run = subprocess.run

    def bounded_run(command, **kwargs):
        converted.append(Path(command[-1]))
        # Bound this regression test even if the format restriction regresses.
        kwargs["timeout"] = min(kwargs["timeout"], 1.0)
        return real_run(command, **kwargs)

    monkeypatch.setattr("weaver.spinner.stt.subprocess.run", bounded_run)
    with pytest.raises(AudioDecodeError) as error:
        MlxStt(model, engine=lambda *_: pytest.fail("engine called")).transcribe(audio)
    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)
    assert b"whitelist" in error.value.__cause__.stderr
    assert not converted[0].parent.exists()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_decode_deadline_is_rejected(model, timeout):
    with pytest.raises(ValueError, match="decode_timeout"):
        MlxStt(model, decode_timeout=timeout)


@pytest.mark.parametrize("missing", ["mlx_whisper", "numpy"])
def test_missing_optional_dependency_fails_at_engine_creation(
    model, monkeypatch, missing
):
    monkeypatch.setattr(
        "weaver.spinner.stt.importlib.util.find_spec",
        lambda name: None if name == missing else object(),
    )
    with pytest.raises(SttEngineError) as error:
        MlxStt(model)
    assert missing in str(error.value)
    assert "uv sync --project server --locked --extra stt" in str(error.value)


def test_engine_creation_only_checks_presence(model, monkeypatch):
    monkeypatch.setattr(
        "weaver.spinner.stt.importlib.util.find_spec", lambda _: object()
    )
    monkeypatch.setattr(
        "weaver.spinner.stt.importlib.import_module",
        lambda _: pytest.fail("constructor imported optional runtime"),
    )
    MlxStt(model)


def test_transitive_import_failure_names_missing_module(
    model, audio, decoder, monkeypatch
):
    monkeypatch.setattr(
        "weaver.spinner.stt.importlib.util.find_spec", lambda _: object()
    )

    def missing(name):
        raise ModuleNotFoundError("missing transitive dependency", name="mlx.core")

    monkeypatch.setattr("weaver.spinner.stt.importlib.import_module", missing)
    stt = MlxStt(model)
    with pytest.raises(SttEngineError) as error:
        stt.transcribe(audio)
    assert "mlx.core" in str(error.value)
    assert "uv sync --project server --locked --extra stt" in str(error.value)


def test_importing_stt_and_preparation_never_loads_optional_dependencies():
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys

class NoSttImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"mlx", "mlx_whisper", "numpy", "huggingface_hub"}:
            raise AssertionError(f"Unexpected optional import: {fullname}")

sys.meta_path.insert(0, NoSttImports())
import weaver.spinner.stt
import weaver.spinner.prepare_model
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
