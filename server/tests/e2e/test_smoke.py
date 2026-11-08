"""File-protocol smoke through the same assembly used by ``weaver run``.

Only the LlmClient is replaced. Phone capture/review producers write synthetic
Courier files; all stores, parsers, review, feedback and vault output are real.
ADR-028 defers audio: Galaxy call scripts are text and never invoke local STT.
"""

import copy
import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from weaver.config import load_config
from weaver.fabric import EntryQuery, FabricStore
from weaver.loom import ReviewStore, SqliteFeedbackStore
from weaver.runtime.assemble import Plugins, build_runtime
from weaver.schema import Manifest, Proposal, Receipt, Verdict
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
        assert task in {"loom.extract", "loom.revise"}
        if task == "loom.extract":
            if self.fail_extract:
                raise OSError("synthetic transient failure")
            data = example("Proposal")
            known = {e["name"]: e["entity_id"] for e in payload["known"]["entities"]}
            for entity, name in zip(data["entities"], self.names, strict=True):
                entity["name"] = name
                entity["aliases"] = []
                entity["thread_excerpt"] = next(
                    (
                        c["context"]
                        for c in payload["candidates"]
                        if name in c["context"]
                    ),
                    EXCERPT,
                )
                entity.update(entity_id=known.get(entity["name"]), ask=None)
            data["relations"][0]["reason"] = EXCERPT
            if self.low_confidence:
                data["entities"][0]["confidence"] = 0.5
        else:
            data = copy.deepcopy(payload["proposal"])
            data["summary"] = "다시 검토한 점검 회의 요청"
        for key in ("schema_version", "proposal_id", "rev", "source"):
            data.pop(key)
        for draft in data["entry_drafts"]:
            draft["value"] = json.dumps(draft["value"], ensure_ascii=False)
        return schema.model_validate(data)

    def generate_text(self, *args, **kwargs):
        pytest.fail("pipeline must use structured LLM calls")


class World:
    def __init__(self, config):
        self.config = config
        self.paths = config.courier
        self.client = DeterministicLlm()
        self.now = datetime.fromisoformat(AT).timestamp()
        self.restart()
        self.fabric = FabricStore.from_config(config)
        self.reviews = ReviewStore.from_config(config)
        self.feedback = SqliteFeedbackStore.from_config(config, self.reviews)
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
        assert self.runtime.publisher.failures == 0
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
            read_wire(p, Proposal)
            for p in sorted(self.paths.proposals.glob("prp_*.json"))
        ]

    def proposal(self):
        proposals = self.proposals()
        assert len(proposals) == 1
        return proposals[0]

    def decide(self, proposal, kind="approve", **changes):
        data = example("Verdict") | {
            "proposal_id": proposal.proposal_id,
            "rev": proposal.rev,
            "verdict": kind,
            "reviewed_at": AT,
        }
        if kind == "reject":
            data["reject_reason"] = "not_useful"
        if kind == "revise":
            data["instruction"] = "요청의 의미를 보존하고 다시 검토"
        data.update(changes)
        verdict = Verdict.model_validate(data)
        path = self.paths.decisions / f"{verdict.proposal_id}.r{verdict.rev}.json"
        atomic_json(path, data)
        assert read_wire(path, Verdict) == verdict
        return path

    def entries(self):
        return self.fabric.entries(EntryQuery())

    def vault(self):
        return {
            p.relative_to(self.config.vault_root): p.read_bytes()
            for p in self.config.vault_root.rglob("*")
            if p.is_file()
        }

    def untouched(self):
        assert self.entries() == []
        assert self.vault() == self.initial_vault

    def cleaned(self):
        assert not self.proposals()
        assert not list(self.paths.decisions.glob("*.json"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    def forbid_stt(*args, **kwargs):
        pytest.fail("ADR-028: runtime must not construct or call STT")

    monkeypatch.setattr(MlxStt, "__init__", forbid_stt)
    monkeypatch.setattr(MlxStt, "transcribe", forbid_stt)

    # Freeze Entry recording, so Fabric ordering and rendered timestamps are stable.
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(AT).astimezone(tz or UTC)

    monkeypatch.setattr("weaver.loom.propose.datetime", FixedDatetime)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
        + '\n[llm]\nbackend = "none"\n[publish]\nenabled = true\n'
    )
    return World(load_config(config_file, environ={}, init_directories=True))


@pytest.mark.parametrize("channel,text", [("share_text", TEXT), ("call", CALL)])
def test_text_and_galaxy_script_use_real_assembly_without_stt(world, channel, text):
    manifest = world.capture(channel=channel, text=text)
    world.tick()
    receipt = world.receipt(manifest)
    proposal = world.proposal()
    assert proposal.source.source_id == receipt.source_id
    assert proposal.source.channel == channel
    assert proposal.thread_excerpt == EXCERPT
    assert proposal.schedules[0].blocked_by_request == "d1"
    assert proposal.entities[0].confidence == 0.92
    assert proposal.entities[0].ask is None
    tasks = [task for task, _ in world.client.calls]
    assert tasks == (
        ["spinner.revise", "loom.extract"] if channel == "call" else ["loom.extract"]
    )
    if channel == "call":
        speech = world.client.calls[0][1]["segments"]
        assert [s["text"].strip() for s in speech] == [
            TEXT,
            "금요일까지 자료를 보내 주세요.",
        ]
        assert [s["speaker"] for s in speech] == ["나", "김민수"]
        assert [s["start"] for s in speech] == [1.0, 3.0]
    world.untouched()  # includes the real OutputWatcher scan


def test_fleece_is_archived_but_permanently_unsupported(world):
    manifest = world.capture(
        kind="fleece", channel="call", text="synthetic audio bytes"
    )
    world.tick()
    receipt = world.receipt(manifest)
    assert world.runtime.ledger.failures() == [
        {"source_id": receipt.source_id, "error_class": "SpinnerError"}
    ]
    assert not world.client.calls and not world.proposals()
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
        bad = manifest | (
            {"sha256": "0" * 64} if invalid == "hash" else {"schema_version": 99}
        )
        atomic_json(path, bad)
    else:
        prefix = ".syncthing." if invalid == "temporary" else "."
        path.rename(
            path.with_name(
                prefix + path.name + (".tmp" if invalid == "temporary" else "")
            )
        )
    world.tick()
    assert not world.proposals() and not world.client.calls
    assert not world.runtime.feed.sources()
    assert not list(world.paths.receipts.iterdir())
    world.untouched()
    atomic_json(path, manifest)
    world.tick()
    world.receipt(manifest)
    first = world.proposal()
    world.tick()
    assert world.proposal() == first
    assert len(world.runtime.feed.sources()) == len(world.client.calls) == 1
    world.untouched()


@pytest.mark.parametrize("kind", ["approve", "edit", "reject"])
def test_review_fabric_feedback_and_vault_roundtrip(world, kind):
    manifest = world.capture()
    world.tick()
    proposal = world.proposal()
    world.untouched()
    edits = (
        [{"path": "/entry_drafts/1/value", "value": "수정한 회의 요청"}]
        if kind == "edit"
        else []
    )
    decision = world.decide(proposal, kind, edits=edits)
    decision_bytes = decision.read_bytes()
    world.tick()
    world.cleaned()
    event = world.feedback.event(proposal.proposal_id, 1)
    assert event["verdict"] == kind
    if kind == "reject":
        assert event["reject_reason"] == "not_useful"
        world.untouched()
    else:
        entries = world.entries()
        assert len(entries) == 2
        assert all(
            e.actor == "ai_approved" and e.source_id == proposal.source.source_id
            for e in entries
        )
        request = next(e for e in entries if e.kind == "요청")
        schedule = next(e for e in entries if e.kind == "일정")
        assert schedule.blocked_by_request == request.entry_id
        if kind == "edit":
            assert schedule.value == "수정한 회의 요청"
            assert any(
                c["path"] == edits[0]["path"] and c["after"] == edits[0]["value"]
                for c in event["corrections"]
            )
        notes = "\n".join(
            b.decode() for p, b in world.vault().items() if p != Path("Human.md")
        )
        for entity in request.entities:
            state = world.fabric.state(entity)
            assert [e.entry_id for e in state.open_requests] == [request.entry_id]
            assert not state.decisions
            assert {e.entry_id for e in state.accumulated} == {
                request.entry_id,
                schedule.entry_id,
            }
        assert request.request.content in notes
        assert str(schedule.value) in notes
        assert request.source_id in notes and request.entry_id in notes
        assert all(
            p.parts[0] == "Weaver" for p in world.vault() if p != Path("Human.md")
        )
    before = world.vault()
    before_entries = world.entries()
    # Same capture, different capture with identical hash, and review redelivery
    # are replayed across a new runtime, not just an in-memory cache.
    world.capture()
    duplicate = world.capture(2)
    decision.write_bytes(decision_bytes)
    world.restart()
    world.tick()
    assert world.receipt(duplicate).source_id == world.receipt(manifest).source_id
    assert len(world.runtime.feed.sources()) == 1
    assert len(world.client.calls) == 1
    assert world.entries() == before_entries and world.vault() == before
    assert world.feedback.events() == [(proposal.proposal_id, 1)]
    world.cleaned()


def test_revise_stale_review_and_reapproval(world):
    world.capture()
    world.tick()
    first = world.proposal()
    world.decide(first, "revise")
    world.tick()
    second = world.proposal()
    assert second.proposal_id == first.proposal_id and second.rev == first.rev + 1
    assert second.summary == "다시 검토한 점검 회의 요청"
    assert not (world.paths.proposals / f"{first.proposal_id}.r1.json").exists()
    world.untouched()
    stale = world.decide(first)
    report = world.runtime.scanner.scan_once()
    assert "stale_rev" in report.problems.values()
    assert stale.exists()
    world.untouched()
    stale.unlink()  # phone withdraws the invalid stale verdict
    world.restart()
    world.tick()
    assert world.proposal() == second
    world.untouched()
    world.decide(second)
    world.tick()
    assert len(world.entries()) == 2
    assert set(world.feedback.events()) == {
        (first.proposal_id, 1),
        (first.proposal_id, 2),
    }
    world.cleaned()


def test_transient_llm_retry_respects_deadline_after_restart(world):
    world.client.fail_extract = True
    manifest = world.capture()
    world.tick()
    sid = world.receipt(manifest).source_id
    progress = world.runtime.ledger.progress()[sid]
    assert progress.status == "retry" and progress.attempts == 1
    assert progress.next_attempt_at == world.now + 10
    world.untouched()
    world.restart()
    world.client.fail_extract = False
    world.tick()
    assert len(world.client.calls) == 1 and not world.proposals()
    world.now = progress.next_attempt_at
    world.tick()
    assert world.runtime.ledger.counts() == {"done": 1}
    assert len(world.client.calls) == 2
    assert world.proposal().source.source_id == sid
    world.untouched()


def test_low_confidence_requires_edit_before_approval(world):
    world.client.low_confidence = True
    world.capture()
    world.tick()
    proposal = world.proposal()
    assert proposal.entities[0].ask is not None
    path = world.decide(proposal)
    report = world.runtime.scanner.scan_once()
    assert report.problems and path.exists()
    world.untouched()
    world.decide(proposal, "edit", edits=[{"path": "/entities/0/ask", "value": None}])
    world.tick()
    assert len(world.entries()) == 2
    world.cleaned()


def test_publisher_updates_changed_entities_and_preserves_other_notes(world):
    world.capture()
    world.tick()
    world.decide(world.proposal())
    world.tick()
    first_entities = set(world.entries()[0].entities)
    world.client.names = ("박하나", "별도 점검")
    world.capture(2, text=f"박하나 별도 점검: {EXCERPT}")
    world.tick()
    world.decide(world.proposal())
    world.tick()
    # Keep unrelated generated notes and a human note inside the AI folder.
    human_ai = world.config.vault_root / "Weaver" / "Personal.md"
    human_ai.write_text("사람이 직접 쓴 AI 폴더 메모\n")
    before = world.vault()
    stamps = {p: (world.config.vault_root / p).stat().st_mtime_ns for p in before}
    world.client.names = ("김민수", "점검")
    world.capture(3, text=TEXT + " 자료도 확인해 주세요.")
    world.tick()
    proposal = world.proposal()
    assert {e.entity_id for e in proposal.entities} == first_entities
    assert world.vault() == before
    world.decide(
        proposal,
        "edit",
        edits=[{"path": "/entry_drafts/1/value", "value": "변경한 점검 회의"}],
    )
    world.tick()
    after = world.vault()
    assert len(world.entries()) == 6
    for path, data in before.items():
        if path.stem in first_entities:
            assert after[path] != data
            assert "변경한 점검 회의" in after[path].decode()
        else:
            assert after[path] == data
            assert (world.config.vault_root / path).stat().st_mtime_ns == stamps[path]
    assert human_ai.read_text() == "사람이 직접 쓴 AI 폴더 메모\n"
    assert all(p.parts[0] == "Weaver" for p in after.keys() - before.keys())


def test_cleanup_io_failure_after_commit_recovers_without_duplicates(
    world, monkeypatch
):
    world.capture()
    world.tick()
    proposal = world.proposal()
    decision = world.decide(proposal)
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path == decision:
            raise OSError("synthetic interruption after approval commit")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", interrupted)
        world.tick()
    assert world.reviews.is_applied(proposal.proposal_id, 1)
    assert decision.exists() and not world.proposals()
    entries, notes = world.entries(), world.vault()
    assert len(entries) == 2
    world.restart()
    world.tick()
    world.cleaned()
    assert world.entries() == entries and world.vault() == notes
    assert world.feedback.events() == [(proposal.proposal_id, 1)]
    assert len(world.client.calls) == 1
