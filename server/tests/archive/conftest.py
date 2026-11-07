"""Archive fixtures. Everything lives in pytest's temporary directory: no
Syncthing, vault, LLM or speech model is touched."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weaver.archive import ArchiveStore
from weaver.schema import Manifest

ROOT = Path(__file__).resolve().parents[3]

CAP = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA0"
CAP2 = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA1"
CAP3 = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA2"
TEXT = "합성 공유 텍스트: 내일 점검 회의를 요청합니다.".encode()
AUDIO = bytes(range(256)) * 64  # synthetic bytes, not real speech


def manifest(capture_id=CAP, body=TEXT, ext="txt", **overrides) -> Manifest:
    raw = json.loads((ROOT / "schema" / "examples" / "Manifest.json").read_text())
    raw |= {
        "capture_id": capture_id,
        "payload": f"{capture_id}.{ext}",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
    }
    raw.update(overrides)
    return Manifest.model_validate(raw)


def outbox_file(outbox: Path, m: Manifest, body: bytes) -> Path:
    path = outbox / m.payload
    path.write_bytes(body)
    return path


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 7, 6, 32, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def dirs(tmp_path):
    layout = {
        "archive": tmp_path / "archive",
        "receipts": tmp_path / "sync" / "receipts",
        "outbox": tmp_path / "sync" / "outbox",
    }
    for path in layout.values():
        path.mkdir(parents=True)
    return layout


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def open_store(dirs, clock):
    def factory(**kwargs):
        return ArchiveStore(dirs["archive"], dirs["receipts"], clock=clock, **kwargs)

    return factory


@pytest.fixture
def store(open_store):
    return open_store()
