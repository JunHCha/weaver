"""File-protocol smoke through the same assembly used by ``weaver run``.

Only the LlmClient is replaced. Phone capture producers write synthetic Courier
files; all stores, parsers, drafting and review journals are real.
ADR-028 defers audio: Galaxy call scripts are text and never invoke local STT.
"""

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from weaver.config import load_config
from weaver.runtime.assemble import Plugins, build_runtime
from weaver.schema import Manifest, Receipt, ReviewPackage
from weaver.spinner.stt import MlxStt

ROOT = Path(__file__).resolve().parents[3]
AT = "2026-11-07T15:32:00+09:00"
EXCERPT = "내일 점검 회의를 요청합니다."
TEXT = f"김민수: {EXCERPT}"
CALL = f"AI로 생성된 콘텐츠입니다\n\n점검 통화\n\n나  (00:01)\n{TEXT}\n\n김민수  (00:03)\n금요일까지 자료를 보내 주세요.\n"


def example(name):
    return json.loads((ROOT / "schema" / "examples" / f"{name}.json").read_text())


def read_wire(path, model):
    data = json.loads(path.read_text())
    schema = json.loads((ROOT / "schema" / f"{model.__name__}.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)
    return model.model_validate(data)


def atomic_json(path, data):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    temporary.replace(path)


class DeterministicLlm:
    """Valid structured replies with real extraction/linking validation afterward."""

    def __init__(self):
        self.calls = []
        self.low_confidence = False
        self.fail_extract = False
        self.names = ("김민수", "점검")

    def generate_json(self, task, system, user, schema):
        payload = json.loads(user)
        self.calls.append((task, payload))
        if task == "spinner.revise":
            return schema.model_validate(
                {
                    "segments": [
                        {"segment_id": s["segment_id"], "text": s["text"]}
                        for s in payload["segments"]
                    ]
                }
            )
        if task == "loom.summary":
            return schema.model_validate(
                {"script": payload["script"], "body": "요약", "questions": []}
            )
        raise AssertionError(task)

    def generate_text(self, *args, **kwargs):
        pytest.fail("pipeline must use structured LLM calls")


class World:
    def __init__(self, config):
        self.config = config
        self.paths = config.courier
        self.client = DeterministicLlm()
        self.now = datetime.fromisoformat(AT).timestamp()
        self.restart()
        self.human = config.vault_root / "Human.md"
        self.human.write_text("사람이 쓴 합성 메모\n")
        self.initial_vault = self.vault()

    def restart(self):
        self.runtime = build_runtime(
            self.config, plugins=Plugins(client=self.client), environ={}
        )
        self.runtime.clock = lambda: self.now
        # One complete run-loop pass, then a completion condition instead of sleep.
        self.runtime._wait = lambda delay: True

    def tick(self):
        self.runtime.run(threading.Event())
        assert self.runtime.consecutive_failures == 0
        assert self.human.read_text() == "사람이 쓴 합성 메모\n"

    def capture(self, n=1, *, text=TEXT, channel="share_text", kind="text"):
        cap = f"cap_{n:026d}"
        body = text.encode()
        extension = "m4a" if kind == "fleece" else "txt"
        data = example("Manifest") | {
            "capture_id": cap,
            "kind": kind,
            "channel": channel,
            "captured_at": AT,
            "occurred_at": AT,
            "payload": f"{cap}.{extension}",
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
        }
        Manifest.model_validate(data)
        # Shepherd contract: finish payload before renaming the manifest.
        (self.paths.outbox / data["payload"]).write_bytes(body)
        atomic_json(self.paths.outbox / f"{cap}.json", data)
        return data

    def receipt(self, manifest):
        result = read_wire(
            self.paths.receipts / f"{manifest['capture_id']}.json", Receipt
        )
        assert result.schema_version == 1
        assert result.capture_id == manifest["capture_id"]
        assert result.sha256 == manifest["sha256"]
        assert result.received_at.utcoffset() is not None
        source = self.runtime.feed.get(result.source_id)
        archived = self.runtime.feed.locate(result.source_id).payload_path
        assert not archived.is_relative_to(self.config.vault_root)
        assert (
            archived.read_bytes()
            == (self.paths.outbox / manifest["payload"]).read_bytes()
        )
        assert source.sha256 == result.sha256
        return result

    def proposals(self):
        return [
            ReviewPackage.model_validate_json(p.read_bytes())
            for p in sorted(self.paths.proposals.glob("*.json"))
        ]

    def vault(self):
        return {
            p.relative_to(self.config.vault_root): p.read_bytes()
            for p in self.config.vault_root.rglob("*")
            if p.is_file()
        }

    def untouched(self):
        assert self.vault() == self.initial_vault


@pytest.fixture
def world(tmp_path, monkeypatch):
    def forbid_stt(*args, **kwargs):
        pytest.fail("ADR-028: runtime must not construct or call STT")

    monkeypatch.setattr(MlxStt, "__init__", forbid_stt)
    monkeypatch.setattr(MlxStt, "transcribe", forbid_stt)

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
        + '\n[llm]\nbackend = "none"\n'
    )
    return World(load_config(config_file, environ={}, init_directories=True))


def test_fleece_is_archived_but_permanently_unsupported(world):
    manifest = world.capture(
        kind="fleece", channel="call", text="synthetic audio bytes"
    )
    world.tick()
    receipt = world.receipt(manifest)
    assert world.runtime.ledger.failures() == [
        {"source_id": receipt.source_id, "error_class": "SpinnerError"}
    ]
    world.restart()
    world.tick()
    assert world.runtime.ledger.counts() == {"failed": 1}
    world.untouched()


@pytest.mark.parametrize(
    "invalid", ["missing_manifest", "hash", "version", "temporary", "hidden"]
)
def test_incomplete_or_invalid_capture_normalizes_once(world, invalid):
    manifest = world.capture()
    path = world.paths.outbox / f"{manifest['capture_id']}.json"
    if invalid == "missing_manifest":
        path.unlink()
    elif invalid in {"hash", "version"}:
        atomic_json(
            path,
            manifest
            | ({"sha256": "0" * 64} if invalid == "hash" else {"schema_version": 99}),
        )
    else:
        path.rename(
            path.with_name(
                (".syncthing." if invalid == "temporary" else ".")
                + path.name
                + (".tmp" if invalid == "temporary" else "")
            )
        )
    world.tick()
    assert not world.proposals() and not world.client.calls
    assert not world.runtime.feed.sources()
    assert not list(world.paths.receipts.iterdir())
    atomic_json(path, manifest)
    world.tick()
    world.receipt(manifest)
    first = world.proposals()
    assert len(first) == 1
    world.tick()
    assert world.proposals() == first
    assert len(world.runtime.feed.sources()) == len(world.client.calls) == 1
    world.untouched()
