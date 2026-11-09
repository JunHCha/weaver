"""Review contracts using isolated files, SQLite and injected fake LLM/sink."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from weaver.config import CourierPaths
from weaver.courier import Courier
from weaver.fabric import EntryQuery, FabricStore
from weaver.llm import (
    InvalidJson,
    LlmTimeout,
    MissingOutput,
    Refusal,
    SchemaMismatch,
    UsageLimited,
)
from weaver.loom import (
    CourierPublisher,
    CourierReviewAdapter,
    Proposer,
    ReviewStore,
    VerdictHandler,
)
from weaver.loom.propose import MAX_REVISE_FAILURES
from weaver.runtime.gate import LlmUnavailable
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
    source.source_id = "src_01ARZ3NDEKTSV4RRFFQ69G5FA9"
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


@pytest.mark.parametrize(
    "mutation",
    [
        "rename_unblock",
        "remove_request",
        "request_content",
        "one_character",
        "swap_block",
    ],
)
def test_revision_safety_regressions(env, mutation):
    p = env.proposal.model_copy(deep=True)
    p.entry_drafts[0].ref = "d3"
    p.entry_drafts[1].ref = "d4"
    p.entry_drafts[1].blocked_by_request = "d3"
    p.schedules[0].ref = "s9"
    p.schedules[0].entry_ref = "d4"
    p.schedules[0].blocked_by_request = "d3"
    if mutation == "rename_unblock":
        p.entry_drafts[1].blocked_by_request = None
        p.schedules[0].blocked_by_request = None
    elif mutation == "remove_request":
        p.entry_drafts[0].kind = "논의"
        p.entry_drafts[0].request = None
        p.entry_drafts[1].blocked_by_request = None
        p.schedules[0].blocked_by_request = None
    elif mutation == "request_content":
        p.entry_drafts[0].request.content = "다른 요청"
    elif mutation == "one_character":
        p.thread_excerpt = p.thread_excerpt[0]
    else:
        extra = p.entry_drafts[1].model_copy(deep=True)
        extra.ref = "d5"
        extra.blocked_by_request = None
        p.entry_drafts.append(extra)
    env.client.proposal = p
    with pytest.raises(ValueError):
        env.handler.apply(verdict(env, "revise"))
    assert env.store.review(p.proposal_id, 1) is None


def test_revision_can_rename_refs_without_losing_blocks(env):
    p = env.client.proposal.model_copy(deep=True)
    p.entry_drafts[0].ref = "d3"
    p.entry_drafts[1].blocked_by_request = "d3"
    p.schedules[0].blocked_by_request = "d3"
    env.client.proposal = p
    assert env.handler.apply(verdict(env, "revise")).rev == 2


def test_build_failure_retry_uses_source_identity(env, monkeypatch):
    source = Source.model_validate(example("Source"))
    source.source_id = "src_01ARZ3NDEKTSV4RRFFQ69G5FA9"
    data = env.proposal.model_dump(
        mode="json", exclude={"schema_version", "proposal_id", "rev", "source"}
    )
    extraction = Extraction.model_validate(data)
    original = env.publisher.publish_proposal
    monkeypatch.setattr(
        env.publisher,
        "publish_proposal",
        lambda *a: (_ for _ in ()).throw(OSError("private detail")),
    )
    with pytest.raises(RuntimeError) as error:
        Proposer(env.store, env.publisher).build(source, extraction)
    saved = env.store.source_proposal(source.source_id)
    assert saved.proposal_id in str(error.value)
    monkeypatch.setattr(env.publisher, "publish_proposal", original)
    assert (
        Proposer(ReviewStore(env.store.path), env.publisher).build(source, extraction)
        == saved
    )
    assert Proposer(env.store, env.publisher).build(source, extraction) == saved
    env.handler.apply(
        Verdict.model_validate(example("Verdict") | {"proposal_id": saved.proposal_id})
    )
    assert Proposer(env.store, env.publisher).build(source, extraction) == saved
    assert not (env.paths.proposals / f"{saved.proposal_id}.r1.json").exists()


def test_approved_snapshot_mapping_and_edits_survive_restart(env):
    p = env.proposal.model_copy(deep=True)
    p.rev = 2
    extra = p.entities[0].model_copy(deep=True)
    extra.ref, extra.name = "e9", "연결 없는 대상"
    p.entities.append(extra)
    env.store.save_proposal(p)
    env.publisher.publish_proposal(p)
    decision = verdict(
        env,
        "edit",
        rev=2,
        edits=[
            {"path": "/summary", "value": "수정된 요약"},
            {"path": "/entities/0/name", "value": "수정된 이름"},
        ],
    )
    env.sink.fail = True
    with pytest.raises(RuntimeError):
        env.handler.apply(decision)
    approved, ids = env.store.approval(p.proposal_id, 2)
    assert approved.summary == "수정된 요약"
    assert approved.entities[0].name == "수정된 이름"
    assert set(ids) == {"e1", "e2", "e9"}
    assert all(e.entity_id == ids[e.ref] for e in approved.entities)
    env.handler.store = ReviewStore(env.store.path)
    env.sink.fail = False
    env.handler.apply(decision)
    event = env.sink.events[(p.proposal_id, 2)]
    assert event.proposal == approved
    assert event.entity_ids == ids
    assert env.store.proposal(p.proposal_id, 2).summary != approved.summary


def test_direct_r2_apply_refuses_prepared_r1(env):
    decision = verdict(env, "revise")
    env.sink.fail = True
    with pytest.raises(RuntimeError):
        env.handler.apply(decision)
    env.sink.fail = False
    with pytest.raises(ValueError, match="PREPARED"):
        env.handler.apply(verdict(env, rev=2))
    assert not env.store.review(decision.proposal_id, 2)
    env.handler.apply(decision)
    env.handler.apply(verdict(env, rev=2))
    assert not list(env.paths.proposals.iterdir())


@pytest.mark.parametrize("path", ["/entities/0/re~0f", "/entities/0/re~1f"])
def test_escaped_identity_paths_refused(env, path):
    with pytest.raises(ValueError):
        env.handler.apply(verdict(env, "edit", edits=[{"path": path, "value": "e9"}]))
    assert not env.store.review(env.proposal.proposal_id, 1)


def test_duplicate_edit_paths_validate_final_payload(env):
    with pytest.raises(ValueError):
        env.handler.apply(
            verdict(
                env,
                "edit",
                edits=[
                    {"path": "/summary", "value": "valid"},
                    {"path": "/summary", "value": 12},
                ],
            )
        )
    assert not env.store.review(env.proposal.proposal_id, 1)


def test_escaped_tilde_value_key_and_schedule_ask_preserved(env):
    p = env.proposal.model_copy(deep=True)
    p.rev = 2
    p.schedules[0].ask = Ask(question="일정을 확인하세요", choices=[])
    env.store.save_proposal(p)
    result = env.handler.apply(
        verdict(
            env,
            "edit",
            rev=2,
            edits=[
                {"path": "/entry_drafts/0/value", "value": {"a~b": 1}},
                {"path": "/entry_drafts/0/value/a~0b", "value": 2},
            ],
        )
    )
    assert result[0].value == {"a~b": 2}
    assert (
        env.sink.events[(p.proposal_id, 2)].proposal.schedules[0].ask
        == p.schedules[0].ask
    )


def test_recovery_warning_contains_type_identity_only(env, caplog):
    decision = verdict(env, "revise")
    env.sink.fail = True
    with pytest.raises(RuntimeError):
        env.handler.apply(decision)
    env.courier.scan_once()
    assert "RuntimeError" in caplog.text
    assert decision.proposal_id in caplog.text
    assert "sink interrupted" not in caplog.text
    assert env.proposal.thread_excerpt not in caplog.text


def test_build_same_source_never_duplicates_proposals(env):
    source = Source.model_validate(example("Source"))
    data = env.proposal.model_dump(
        mode="json", exclude={"schema_version", "proposal_id", "rev", "source"}
    )
    extraction = Extraction.model_validate(data)
    a = Proposer(env.store, env.publisher).build(source, extraction)
    b = Proposer(env.store, env.publisher).build(source, extraction)
    assert a.proposal_id == b.proposal_id == env.proposal.proposal_id


def test_feedback_contains_actual_edited_proposal(env):
    env.handler.apply(
        verdict(
            env, "edit", edits=[{"path": "/summary", "value": "검토자가 바꾼 요약"}]
        )
    )
    event = env.sink.events[(env.proposal.proposal_id, 1)]
    assert event.proposal.summary == "검토자가 바꾼 요약"
    assert event.proposal.entities[0].entity_id is not None


def test_v1_journal_migrates_source_lookup(env, tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE proposals (proposal_id TEXT, rev INTEGER, snapshot TEXT, PRIMARY KEY(proposal_id, rev)); CREATE TABLE reviews (proposal_id TEXT, rev INTEGER, verdict TEXT, result TEXT, stage TEXT, PRIMARY KEY(proposal_id, rev)); PRAGMA user_version=1;"
        )
        db.execute(
            "INSERT INTO proposals VALUES (?,?,?)",
            (env.proposal.proposal_id, 1, env.proposal.model_dump_json()),
        )
    store = ReviewStore(path)
    assert store.source_proposal(env.proposal.source.source_id) == env.proposal
    with store.connect() as db:
        assert (
            db.execute("PRAGMA user_version").fetchone()[0] == 3
        )  # v2 source lookup, then #27 versions
    assert (
        ReviewStore(path).source_proposal(env.proposal.source.source_id) == env.proposal
    )


def failing_client(env, monkeypatch, error):
    calls = []

    def generate_json(task, system, user, schema):
        calls.append(task)
        raise error

    monkeypatch.setattr(env.client, "generate_json", generate_json)
    return calls


@pytest.mark.parametrize(
    "error",
    [
        ValueError("revision evidence outside supplied source"),
        LlmTimeout(),
        InvalidJson(),
        SchemaMismatch(),
        MissingOutput(),
        Refusal(),
    ],
    ids=lambda error: type(error).__name__,
)
def test_failed_revise_stops_calling_llm_after_limit(env, monkeypatch, error):
    decision = verdict(env, "revise")
    calls = failing_client(env, monkeypatch, error)
    for _ in range(MAX_REVISE_FAILURES + 3):
        report = env.courier.scan_once()
    assert len(calls) == MAX_REVISE_FAILURES
    assert set(report.problems.values()) == {"revise_exhausted"}
    assert env.store.review(decision.proposal_id, 1) is None
    assert list(env.paths.decisions.iterdir())
    with pytest.raises(ValueError, match="exhausted"):
        env.handler.apply(decision)
    assert len(calls) == MAX_REVISE_FAILURES


def test_revise_failure_count_survives_restart(env, monkeypatch):
    verdict(env, "revise")
    calls = failing_client(env, monkeypatch, LlmTimeout())
    for _ in range(MAX_REVISE_FAILURES - 1):
        env.courier.scan_once()
    env.handler.store = ReviewStore(env.store.path)
    for _ in range(3):
        env.courier.scan_once()
    assert len(calls) == MAX_REVISE_FAILURES


@pytest.mark.parametrize("error", [UsageLimited(), LlmUnavailable()])
def test_unavailable_llm_does_not_spend_revise_attempts(env, monkeypatch, error):
    decision = verdict(env, "revise")
    calls = failing_client(env, monkeypatch, error)
    for _ in range(MAX_REVISE_FAILURES + 2):
        report = env.courier.scan_once()
    assert len(calls) == MAX_REVISE_FAILURES + 2
    assert "revise_exhausted" not in report.problems.values()
    monkeypatch.undo()
    env.courier.scan_once()
    assert env.store.latest_rev(decision.proposal_id) == 2


@pytest.mark.parametrize(
    "kind,changes",
    [("reject", {}), ("revise", {"instruction": "요청 내용은 유지하고 다시 확인"})],
)
def test_exhausted_revise_allows_a_different_verdict(env, monkeypatch, kind, changes):
    decision = verdict(env, "revise")
    failing_client(env, monkeypatch, ValueError("invalid"))
    for _ in range(MAX_REVISE_FAILURES):
        env.courier.scan_once()
    monkeypatch.undo()
    verdict(env, kind, **changes)
    report = env.courier.scan_once()
    assert report.problems == {}
    assert env.store.is_applied(decision.proposal_id, 1)
