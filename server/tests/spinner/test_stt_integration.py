"""Manual only: WEAVER_RUN_LOCAL_STT=1, WEAVER_STT_MODEL, WEAVER_STT_AUDIO.

WEAVER_STT_AUDIO must be an explicitly approved short Korean fixture (not user
vault audio). Run with the network disabled at OS level after preparing the
model. This suite never downloads weights; default CI always skips it.
"""

import os
import platform
import socket
import wave
from pathlib import Path

import pytest

from weaver.spinner.stt import MlxStt

pytestmark = [
    pytest.mark.local_stt,
    pytest.mark.skipif(
        os.environ.get("WEAVER_RUN_LOCAL_STT") != "1",
        reason="manual model smoke requires explicit WEAVER_RUN_LOCAL_STT=1",
    ),
]


def test_offline_korean_and_silence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert platform.system() == "Darwin" and platform.machine() == "arm64"
    model = Path(os.environ["WEAVER_STT_MODEL"])
    audio = Path(os.environ["WEAVER_STT_AUDIO"])
    expected = os.environ["WEAVER_STT_EXPECTED_TEXT"]
    assert expected.strip(), "Supply a known Korean phrase from the approved fixture"

    def deny_network(*args, **kwargs):
        raise AssertionError("Network access during offline smoke")

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    stt = MlxStt(model)
    original = audio.read_bytes()
    result = stt.transcribe(audio)
    assert expected in " ".join(s.text for s in result.segments)
    assert all(s.speaker is None for s in result.segments)
    assert audio.read_bytes() == original
    silence = tmp_path / "silence.wav"
    with wave.open(str(silence), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(bytes(16000 * 2))
    assert stt.transcribe(silence).segments == ()
