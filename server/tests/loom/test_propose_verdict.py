"""Review contracts using isolated files, SQLite and injected fake LLM/sink."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from weaver.config import CourierPaths
from weaver.courier import Courier
from weaver.fabric import EntryQuery, FabricStore
from weaver.loom import (
    CourierPublisher,
    CourierReviewAdapter,
    Proposer,
    ReviewStore,
    VerdictHandler,
)
from weaver.schema import Proposal, Source, Verdict
from weaver.schema.internal import Extraction
from weaver.schema.models import Ask

ROOT = Path(__file__).resolve().parents[3]


def example(name):
    return json.loads((ROOT / "schema" / "examples" / f"{name}.json").read_text())


class Sink:
    def __init__(self):
        self.events = {}
        self.fail = False

    def record(self, event):
        if self.fail:
            raise RuntimeError("sink interrupted")
        self.events.setdefault(event.event_id, event)


class Client:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = 0
        self.fail = False

    def generate_json(self, task, system, user, schema):
        self.calls += 1
        assert task == "loom.revise"
        if self.fail:
            raise RuntimeError("LLM unavailable")
        data = self.proposal.model_dump(
            mode="json", exclude={"schema_version", "proposal_id", "rev", "source"}
        )
        for draft in data["entry_drafts"]:
            draft["value"] = json.dumps(draft["value"])
        return schema.model_validate(data)


@pytest.fixture
def env(tmp_path):
    paths = CourierPaths(
        **{
            n: tmp_path / "sync" / n
            for n in ("outbox", "receipts", "proposals", "decisions")
        }
    )
    for path in (paths.outbox, paths.receipts, paths.proposals, paths.decisions):
        path.mkdir(parents=True)
    store = ReviewStore(tmp_path / "state" / "loom.sqlite3")
    proposal = Proposal.model_validate(example("Proposal"))
    proposal.entities[0].ask = None
    store.save_proposal(proposal)
    fabric = FabricStore(tmp_path / "state" / "fabric.sqlite3")
    sink, client = Sink(), Client(proposal)
    # Bind scan ports after constructing the publisher cycle.
    publisher = CourierPublisher(store)
    handler = VerdictHandler(store, fabric, publisher, sink, client)
    adapter = CourierReviewAdapter(handler)
    courier = Courier(paths, None, adapter, adapter)
    publisher.courier = courier
    publisher.publish_proposal(proposal)
    return SimpleNamespace(**locals())


def verdict(env, kind="approve", **changes):
    data = example("Verdict") | {
        "proposal_id": env.proposal.proposal_id,
        "verdict": kind,
    }
    if kind == "reject":
        data["reject_reason"] = "not_useful"
    elif kind == "revise":
        data["instruction"] = "다시 확인"
    data.update(changes)
    result = Verdict.model_validate(data)
    (env.paths.decisions / f"{result.proposal_id}.r{result.rev}.json").write_text(
        result.model_dump_json()
    )
    return result


def test_build_publishes_r1_with_provenance(env):
    source = Source.model_validate(example("Source"))
    data = example("Proposal")
    extraction = Extraction.model_validate(
        {
            k: data[k]
            for k in (
                "summary",
                "thread_excerpt",
                "entities",
                "schedules",
                "relations",
                "entry_drafts",
            )
        }
    )
    proposal = Proposer(env.store, env.publisher).build(source, extraction)
    assert proposal.rev == 1
    assert proposal.entities[0].ask == extraction.entities[0].ask
    assert proposal.entry_drafts == extraction.entry_drafts
    assert env.store.proposal(proposal.proposal_id, 1) == proposal
    assert (env.paths.proposals / f"{proposal.proposal_id}.r1.json").exists()
    assert env.store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["approve", "edit", "reject", "revise"])
def test_four_verdicts_and_idempotency(env, kind):
    changes = (
        {"edits": [{"path": "/summary", "value": "수정"}]} if kind == "edit" else {}
    )
    decision = verdict(env, kind, **changes)
    result = env.handler.apply(decision)
    assert isinstance(result, Proposal) == (kind == "revise")
    assert len(env.fabric.entries(EntryQuery())) == (
        2 if kind in ("approve", "edit") else 0
    )
    if kind in ("approve", "edit"):
        assert result[0].kind == "요청"
        assert result[1].blocked_by_request == result[0].entry_id
        assert all(e.resolves is None and e.actor == "ai_approved" for e in result)
        assert not env.fabric.state(result[0].entities[0]).decisions
    assert not list(env.paths.decisions.iterdir())
    remaining = list(env.paths.proposals.iterdir())
    assert len(remaining) == (1 if kind == "revise" else 0)
    if kind == "revise":
        assert result.proposal_id == decision.proposal_id and result.rev == 2
        assert remaining[0].name.endswith(".r2.json")
    assert env.handler.apply(decision) == result
    assert len(env.sink.events) == 1
    assert env.client.calls == (1 if kind == "revise" else 0)


@pytest.mark.parametrize("group", ["entities", "relations", "entry_drafts"])
def test_ask_blocks_entire_approval(env, group):
    proposal = env.proposal.model_copy(deep=True)
    getattr(proposal, group)[0].ask = Ask.model_validate(
        example("Proposal")["entities"][0]["ask"]
    )
    proposal = Proposal.model_validate_json(proposal.model_dump_json())
    # New isolated revision so immutable snapshot is respected.
    proposal.rev = 2
    env.store.save_proposal(proposal)
    env.publisher.publish_proposal(proposal)
    decision = verdict(env, rev=2)
    with pytest.raises(ValueError, match="ask"):
        env.handler.apply(decision)
    assert not env.fabric.entries(EntryQuery())
    assert list(env.paths.decisions.iterdir()) and list(env.paths.proposals.iterdir())
    edit = verdict(
        env, "edit", rev=2, edits=[{"path": f"/{group}/0/ask", "value": None}]
    )
    assert len(env.handler.apply(edit)) == 2


@pytest.mark.parametrize(
    "path,value",
    [
        ("", {}),
        ("/entities", []),
        ("/entities/0", {}),
        ("/entities/-/name", "x"),
        ("/entities/99/name", "x"),
        ("/entities/00/name", "x"),
        ("/rev", 2),
        ("/source/channel", "call"),
        ("/entities/0/ref", "e3"),
        ("/entities/0/confidence", 1.0),
        ("/entities/0/name", 3),
        ("/entities/0/entity_id", "ent_01ARZ3NDEKTSV4RRFFQ69G5FA9"),
        ("/entry_drafts/0/kind", "결정"),
        ("/entry_drafts/1/kind", "요청"),
        ("/entry_drafts/1/blocked_by_request", None),
        ("/entry_drafts/0/request/content", "x"),
        ("/entry_drafts/0/resolves", None),
        ("/entities/0/ask", {"question": "x", "choices": []}),
        ("/entities/0/aliases/1", "new"),
        ("/entities/0/aliases/-", "new"),
        ("/summary/x", "x"),
        ("/schedules/0/end", "yesterday"),
        ("/entry_drafts/0/entities/0", "e99"),
    ],
)
def test_forbidden_or_invalid_edits_preserve_files(env, path, value):
    decision = verdict(env, "edit", edits=[{"path": path, "value": value}])
    with pytest.raises(ValueError):
        env.handler.apply(decision)
    assert not env.store.review(decision.proposal_id, 1)
    assert not env.fabric.entries(EntryQuery())
    assert list(env.paths.decisions.iterdir()) and list(env.paths.proposals.iterdir())


def test_existing_nested_json_and_alias_replacement(env):
    decision = verdict(
        env,
        "edit",
        edits=[
            {"path": "/entities/0/aliases/0", "value": "김씨"},
            {"path": "/entry_drafts/0/value", "value": {"a/b": [1]}},
            {"path": "/entry_drafts/0/value/a~1b/0", "value": 2},
        ],
    )
    assert env.handler.apply(decision)[0].value == {"a/b": [2]}


@pytest.mark.parametrize("kind", ["approve", "revise", "reject"])
def test_sink_failure_restart_retries_fixed_result(env, kind):
    decision = verdict(env, kind)
    env.sink.fail = True
    with pytest.raises(RuntimeError):
        env.handler.apply(decision)
    assert not env.store.is_applied(decision.proposal_id, 1)
    assert list(env.paths.decisions.iterdir())
    before = env.fabric.entries(EntryQuery())
    env.sink.fail = False
    # Reopen journal and handler: no process-local deduplication.
    env.handler.store = ReviewStore(env.store.path)
    report = env.courier.scan_once()
    assert not report.problems
    assert env.store.is_applied(decision.proposal_id, 1)
    assert env.fabric.entries(EntryQuery()) == before
    assert env.client.calls == (1 if kind == "revise" else 0)
    assert len(env.sink.events) == 1


def test_fabric_success_then_interruption_replays_identical_payload(env, monkeypatch):
    decision = verdict(env)
    append = env.fabric.append

    def interrupted(entries):
        append(entries)
        raise RuntimeError("crash after commit")

    monkeypatch.setattr(env.fabric, "append", interrupted)
    with pytest.raises(RuntimeError):
        env.handler.apply(decision)
    entries = env.fabric.entries(EntryQuery())
    monkeypatch.setattr(env.fabric, "append", append)
    assert env.handler.apply(decision) == entries
    assert env.fabric.entries(EntryQuery()) == entries


def test_cleanup_failure_after_applied_recovers_by_scan(env, monkeypatch):
    decision = verdict(env)
    clean = env.publisher.clean_review
    monkeypatch.setattr(
        env.publisher,
        "clean_review",
        lambda *a: (_ for _ in ()).throw(OSError("crash")),
    )
    with pytest.raises(OSError):
        env.handler.apply(decision)
    assert env.store.is_applied(decision.proposal_id, 1)
    monkeypatch.setattr(env.publisher, "clean_review", clean)
    assert not env.courier.scan_once().problems
    assert not list(env.paths.proposals.iterdir()) and not list(
        env.paths.decisions.iterdir()
    )


def test_llm_failure_preserves_review_and_allows_retry(env):
    decision = verdict(env, "revise")
    env.client.fail = True
    with pytest.raises(RuntimeError):
        env.handler.apply(decision)
    assert env.store.review(decision.proposal_id, 1) is None
    assert env.store.latest_rev(decision.proposal_id) == 1
    assert list(env.paths.proposals.iterdir()) and list(env.paths.decisions.iterdir())
    env.client.fail = False
    assert env.handler.apply(decision).rev == 2


def test_stale_unknown_and_conflicting_verdicts(env):
    decision = verdict(env, "revise")
    env.handler.apply(decision)
    stale = verdict(env)
    report = env.courier.scan_once()
    assert "stale_rev" in report.problems.values()
    assert list(env.paths.decisions.iterdir())
    with pytest.raises(ValueError):
        env.handler.apply(stale)
    with pytest.raises(ValueError):
        env.handler.apply(verdict(env, rev=9))
    with pytest.raises(ValueError):
        env.handler.apply(verdict(env, proposal_id="prp_01ARZ3NDEKTSV4RRFFQ69G5FA9"))


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("verdict", "wat")])
def test_invalid_verdict_at_boundary(env, field, value):
    bad = verdict(env).model_copy(update={field: value})
    with pytest.raises(ValueError):
        env.handler.apply(bad)


def test_entity_choice_uses_original_ask_even_after_acknowledgement(env):
    proposal = Proposal.model_validate(example("Proposal"))
    proposal.rev = 2
    env.store.save_proposal(proposal)
    env.publisher.publish_proposal(proposal)
    chosen = proposal.entities[0].ask.choices[0].entity_id
    decision = verdict(
        env,
        "edit",
        rev=2,
        edits=[
            {"path": "/entities/0/ask", "value": None},
            {"path": "/entities/0/entity_id", "value": chosen},
        ],
    )
    assert env.handler.apply(decision)[0].entities[0] == chosen


@pytest.mark.parametrize("kind", ["revise", "reject"])
def test_unresolved_ask_does_not_block_revise_or_reject(env, kind):
    proposal = Proposal.model_validate(example("Proposal"))
    proposal.rev = 2
    env.store.save_proposal(proposal)
    env.publisher.publish_proposal(proposal)
    assert env.handler.apply(verdict(env, kind, rev=2)) is not None
    assert not env.fabric.entries(EntryQuery())


def test_revise_publish_failure_recovers_before_stale_classification(env, monkeypatch):
    decision = verdict(env, "revise")
    publish = env.publisher.publish_proposal
    monkeypatch.setattr(
        env.publisher,
        "publish_proposal",
        lambda *a: (_ for _ in ()).throw(OSError("crash before publish")),
    )
    with pytest.raises(OSError):
        env.handler.apply(decision)
    assert env.store.latest_rev(decision.proposal_id) == 2
    assert not env.store.is_applied(decision.proposal_id, 1)
    assert next(env.paths.proposals.iterdir()).name.endswith(".r1.json")
    monkeypatch.setattr(env.publisher, "publish_proposal", publish)
    report = env.courier.scan_once()
    assert not report.problems
    assert next(env.paths.proposals.iterdir()).name.endswith(".r2.json")
    assert env.client.calls == 1
    assert env.store.is_applied(decision.proposal_id, 1)


def test_feedback_success_then_applied_commit_failure_deduplicates(env, monkeypatch):
    decision = verdict(env)
    # Model an interruption after sink persistence and before APPLIED by having
    # the sink commit its event then signal a crash to its caller.
    record = env.sink.record

    def interrupted(event):
        record(event)
        raise RuntimeError("crash after feedback commit")

    monkeypatch.setattr(env.sink, "record", interrupted)
    with pytest.raises(RuntimeError):
        env.handler.apply(decision)
    assert not env.store.is_applied(decision.proposal_id, 1)
    monkeypatch.setattr(env.sink, "record", record)
    env.handler.apply(decision)
    assert len(env.sink.events) == 1
    assert len(env.fabric.entries(EntryQuery())) == 2


def test_crash_between_proposal_and_decision_cleanup(env, monkeypatch):
    decision = verdict(env)
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path.parent == env.paths.decisions:
            raise OSError("crash after proposal removed")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(OSError):
        env.handler.apply(decision)
    assert env.store.is_applied(decision.proposal_id, 1)
    assert not list(env.paths.proposals.iterdir())
    assert list(env.paths.decisions.iterdir())
    monkeypatch.setattr(Path, "unlink", unlink)
    assert not env.courier.scan_once().problems
    assert not list(env.paths.decisions.iterdir())


@pytest.mark.parametrize(
    "mutation", ["evidence", "blocked", "unknown_id", "supersedes"]
)
def test_revision_output_cannot_forge_provenance_or_request_lineage(env, mutation):
    changed = env.client.proposal.model_copy(deep=True)
    if mutation == "evidence":
        changed.thread_excerpt = "원본에 없는 근거"
    elif mutation == "blocked":
        changed.entry_drafts[1].blocked_by_request = None
        changed.schedules[0].blocked_by_request = None
    elif mutation == "unknown_id":
        changed.entities[0].entity_id = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA9"
    else:
        changed.entry_drafts[0].supersedes = "evt_01ARZ3NDEKTSV4RRFFQ69G5FA9"
    env.client.proposal = changed
    decision = verdict(env, "revise")
    with pytest.raises(ValueError):
        env.handler.apply(decision)
    assert env.store.review(decision.proposal_id, 1) is None
    assert not env.fabric.entries(EntryQuery())


def test_conflicting_applied_verdict_is_preserved_by_courier(env):
    env.handler.apply(verdict(env))
    verdict(env, "reject")
    report = env.courier.scan_once()
    assert "verdict_conflict" in report.problems.values()
    assert list(env.paths.decisions.iterdir())
    assert len(env.sink.events) == 1


@pytest.mark.parametrize("kind", ["approve", "revise"])
def test_concurrent_duplicate_reviews_serialize_across_handlers(env, kind):
    from concurrent.futures import ThreadPoolExecutor

    decision = verdict(env, kind)
    other = VerdictHandler(
        ReviewStore(env.store.path), env.fabric, env.publisher, env.sink, env.client
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(env.handler.apply, decision)
        b = pool.submit(other.apply, decision)
        assert a.result(timeout=10) == b.result(timeout=10)
    assert len(env.sink.events) == 1
    assert env.client.calls == (1 if kind == "revise" else 0)
    assert len(env.fabric.entries(EntryQuery())) == (2 if kind == "approve" else 0)
