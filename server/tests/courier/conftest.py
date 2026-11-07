"""Neutral fixtures built from schema/examples, plus #26 test doubles.

Nothing here touches Syncthing, a vault, an LLM or a speech model: every test
runs in pytest's temporary directory.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from weaver.config import CourierPaths
from weaver.courier import Courier
from weaver.schema import Proposal, Verdict

ROOT = Path(__file__).resolve().parents[3]

CAP = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA0"
CAP2 = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA1"
PRP = "prp_01ARZ3NDEKTSV4RRFFQ69G5FA0"
PRP2 = "prp_01ARZ3NDEKTSV4RRFFQ69G5FA1"
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


def proposal_for(proposal_id=PRP, rev=1):
    payload = example("Proposal") | {"proposal_id": proposal_id, "rev": rev}
    assert_schema_valid("Proposal", payload)
    return Proposal.model_validate(payload)


def verdict_for(proposal_id=PRP, rev=1, kind="approve"):
    name = "Verdict" if kind == "approve" else f"Verdict-{kind}"
    return copy.deepcopy(example(name)) | {"proposal_id": proposal_id, "rev": rev}


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return path


class FakeCaptures:
    """Durable capture ledger double: completion by capture_id or sha256."""

    def __init__(self):
        self.stored = {}
        self.accepted = []
        self.failures = 0

    def is_captured(self, capture_id, sha256):
        return capture_id in self.stored or sha256 in self.stored.values()

    def accept(self, capture):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("persist failed")
        self.accepted.append(capture)
        self.stored[capture.manifest.capture_id] = capture.manifest.sha256


class FakeRevisions:
    def __init__(self):
        self.latest = {}

    def latest_rev(self, proposal_id):
        return self.latest.get(proposal_id)


class FakeDecisions:
    """VerdictHandler double; ``on_apply`` lets a test emulate a revise."""

    def __init__(self):
        self.applied = set()
        self.calls = []
        self.failures = 0
        self.confirm = True
        self.on_apply = None

    def is_applied(self, proposal_id, rev):
        return (proposal_id, rev) in self.applied

    def apply(self, verdict):
        assert isinstance(verdict, Verdict)
        self.calls.append((verdict.proposal_id, verdict.rev, verdict.verdict))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("apply failed")
        if self.on_apply is not None:
            self.on_apply(verdict)
        if self.confirm:
            self.applied.add((verdict.proposal_id, verdict.rev))


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
