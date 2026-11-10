"""Neutral fixtures built from schema/examples, plus capture and review doubles.

Nothing here touches Syncthing, a vault, an LLM or a speech model: every test
runs in pytest's temporary directory.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from weaver.config import CourierPaths
from weaver.courier import Courier
from weaver.schema import ReviewPackage, Verdict

ROOT = Path(__file__).resolve().parents[3]

CAP = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA0"
CAP2 = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA1"
PKG = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0"
PKG2 = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA1"
BODY = "합성 공유 텍스트: 내일 점검 회의를 요청합니다.".encode()


def example(name):
    return json.loads((ROOT / "schema" / "examples" / f"{name}.json").read_text())


def assert_schema_valid(model_name, payload):
    """Fixtures must match the shared JSON Schema (#22), not just pydantic."""
    schema = json.loads((ROOT / "schema" / f"{model_name}.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def manifest_for(capture_id=CAP, body=BODY, ext="txt", **overrides):
    payload = example("Manifest") | {
        "capture_id": capture_id,
        "payload": f"{capture_id}.{ext}",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
    }
    payload.update(overrides)
    return payload


def package_for(package_id=PKG, revision=1):
    return ReviewPackage(
        schema_version=2,
        package_id=package_id,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        revision=revision,
        draft_body="합성 요약",
        questions=[],
        stage="검토",
        stage_times={"검토": datetime(2026, 11, 7, 6, 32, tzinfo=UTC)},
    )


def verdict_for(package_id=PKG, revision=1, action="approve"):
    return Verdict(
        schema_version=2,
        package_id=package_id,
        revision=revision,
        action=action,
        answers=[],
        edited_body="수정 본문" if action == "edit" else None,
        feedback="이름을 고쳐 주세요" if action == "feedback" else None,
    ).model_dump(mode="json")


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return path


class FakeCaptures:
    """Durable capture ledger double keyed by capture_id (#18 semantics).

    A new capture_id is always "new", even when its body hash is already stored;
    the store then links it to the existing Source and writes its receipt.
    """

    def __init__(self):
        self.stored = {}
        self.accepted = []
        self.confirmed = []
        self.failures = 0

    def status(self, capture_id, sha256):
        if capture_id not in self.stored:
            return "new"
        return "stored" if self.stored[capture_id] == sha256 else "conflict"

    def confirm(self, capture_id):
        assert capture_id in self.stored
        self.confirmed.append(capture_id)

    def accept(self, capture):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("persist failed")
        self.accepted.append(capture)
        self.stored[capture.manifest.capture_id] = capture.manifest.sha256


class FakeRevisions:
    def __init__(self):
        self.latest = {}

    def latest_rev(self, package_id):
        return self.latest.get(package_id)


class FakeDecisions:
    """Review double; ``on_apply`` lets a test emulate a feedback revision."""

    def __init__(self):
        self.applied = set()
        self.calls = []
        self.failures = 0
        self.confirm = True
        self.on_apply = None

    def is_applied(self, package_id, rev):
        return (package_id, rev) in self.applied

    def apply(self, verdict):
        assert isinstance(verdict, Verdict)
        self.calls.append((verdict.package_id, verdict.revision, verdict.action))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("apply failed")
        if self.on_apply is not None:
            self.on_apply(verdict)
        if self.confirm:
            self.applied.add((verdict.package_id, verdict.revision))


@pytest.fixture
def paths(tmp_path):
    sync = tmp_path / "sync"
    folders = {}
    for name in ("outbox", "receipts", "proposals", "decisions"):
        folders[name] = sync / name
        folders[name].mkdir(parents=True)
    return CourierPaths(**folders)


@pytest.fixture
def captures():
    return FakeCaptures()


@pytest.fixture
def revisions():
    return FakeRevisions()


@pytest.fixture
def decisions():
    return FakeDecisions()


@pytest.fixture
def courier(paths, captures, revisions, decisions):
    return Courier(paths, captures, revisions, decisions)


def place_package(paths, package):
    """Write a review package as the server's drafting stage does."""
    path = paths.proposals / f"{package.package_id}.r{package.revision}.json"
    write_json(path, package.model_dump(mode="json"))
    return path
