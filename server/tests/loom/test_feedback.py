"""Feedback storage, alias registry, prompt examples and eval candidates (#27).

Isolated SQLite/files and fake LLM/sink only; no network or user vault.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from weaver.config import CourierPaths, FeedbackSettings
from weaver.courier import Courier
from weaver.fabric import EntryQuery, FabricStore
from weaver.loom import (
    CourierPublisher,
    CourierReviewAdapter,
    FeedbackConflict,
    Proposer,
    ReviewStore,
    SqliteFeedbackStore,
    VerdictHandler,
)
from weaver.loom import eval_candidates as ec
from weaver.loom.extract import LlmExtractor
from weaver.loom.propose import REVISE_PROMPT_VERSION, FeedbackEvent
from weaver.schema import Proposal, Source, Thread, Verdict
from weaver.schema.internal import Extraction

ROOT = Path(__file__).resolve().parents[3]
KIM = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA0"
PROJECT = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA1"
OTHER = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA2"


def example(name):
    return json.loads((ROOT / "schema" / "examples" / f"{name}.json").read_text())


class Client:
    model = "fake-model"

    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = 0

    def generate_json(self, task, system, user, schema):
        self.calls += 1
        data = self.proposal.model_dump(
            mode="json", exclude={"schema_version", "proposal_id", "rev", "source"}
        )
        for draft in data["entry_drafts"]:
            draft["value"] = json.dumps(draft["value"])
        return schema.model_validate(data)


class FlakySink:
    """Wraps the real store: optionally fails before or after persisting."""

    def __init__(self, store):
        self.store = store
        self.fail = None

    def record(self, event):
        if self.fail == "before":
            self.fail = None
            raise RuntimeError("sink interrupted")
        self.store.record(event)
        if self.fail == "after":
            self.fail = None
            raise RuntimeError("crash after feedback commit")


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
    feedback = SqliteFeedbackStore(tmp_path / "state" / "feedback.sqlite3", store)
    fabric = FabricStore(tmp_path / "state" / "fabric.sqlite3")
    proposal = Proposal.model_validate(example("Proposal"))
    client = Client(proposal)
    sink = FlakySink(feedback)
    publisher = CourierPublisher(store)
    handler = VerdictHandler(store, fabric, publisher, sink, client)
    adapter = CourierReviewAdapter(handler)
    courier = Courier(paths, None, adapter, adapter)
    publisher.courier = courier
    return SimpleNamespace(**locals())


def submit(env, proposal, kind="approve", **changes):
    env.store.save_proposal(proposal, {"prompt": "loom.extract.v1", "model": "m"})
    env.publisher.publish_proposal(proposal)
    data = example("Verdict") | {
        "proposal_id": proposal.proposal_id,
        "rev": proposal.rev,
        "verdict": kind,
    }
    if kind == "reject":
        data["reject_reason"] = "wrong_person"
    elif kind == "revise":
        data["instruction"] = "김 과장 말고 박 대리로 다시"
    data.update(changes)
    verdict = Verdict.model_validate(data)
    (env.paths.decisions / f"{verdict.proposal_id}.r{verdict.rev}.json").write_text(
        verdict.model_dump_json()
    )
    return verdict


def proposal(pid="prp_01ARZ3NDEKTSV4RRFFQ69G5FA0", **entity0):
    data = example("Proposal") | {"proposal_id": pid}
    data["entities"][0].update(entity0)
    return Proposal.model_validate(data)


LINK = [
    {"path": "/entities/0/ask", "value": None},
    {"path": "/entities/0/entity_id", "value": KIM},
]


def test_store_is_private_and_records_edit_once(env):
    decision = submit(
        env,
        proposal(),
        "edit",
        edits=[*LINK, {"path": "/entities/1/type", "value": "topic"}],
    )
    entries = env.handler.apply(decision)
    assert env.feedback.path.stat().st_mode & 0o777 == 0o600
    record = env.feedback.event(decision.proposal_id, 1)
    assert record["outcome"] == "entries"
    assert record["result"] == [e.entry_id for e in entries]
    assert record["versions"] == {"prompt": "loom.extract.v1", "model": "m"}
    kinds = [(c["kind"], c["before"], c["after"]) for c in record["corrections"]]
    assert kinds == [
        ("entities.ask", record["corrections"][0]["before"], None),
        ("entities.entity_id", None, KIM),
        ("entities.type", "project", "topic"),
    ]
    # Only edited paths keep values; the full snapshot stays in the journal.
    assert "snapshot" not in json.dumps(record, ensure_ascii=False)
    assert env.handler.apply(decision) == entries
    assert env.feedback.events() == [(decision.proposal_id, 1)]


@pytest.mark.parametrize(
    "kind,outcome", [("reject", "rejected"), ("revise", "revised")]
)
def test_reject_and_revise_record_reason_and_instruction(env, kind, outcome):
    decision = submit(env, proposal(), kind)
    env.handler.apply(decision)
    record = env.feedback.event(decision.proposal_id, 1)
    assert record["outcome"] == outcome
    assert record["reject_reason"] == decision.reject_reason
    assert record["instruction"] == decision.instruction
    assert not env.feedback.index().entities  # never changes identities
    if kind == "revise":
        assert record["result"] == {"next_rev": 2}
        assert env.store.versions(decision.proposal_id, 2) == {
            "prompt": REVISE_PROMPT_VERSION,
            "model": "fake-model",
        }


def test_conflicting_payload_for_same_event_is_rejected(env):
    decision = submit(env, proposal(), "reject")
    env.handler.apply(decision)
    recorded = env.store.proposal(decision.proposal_id, 1)
    other = decision.model_copy(update={"reject_reason": "duplicate"})
    with pytest.raises(FeedbackConflict):
        env.feedback.record(
            FeedbackEvent((decision.proposal_id, 1), recorded, other, [])
        )


@pytest.mark.parametrize("failure", ["before", "after"])
def test_restart_recovers_feedback_without_duplicate_entries(env, failure):
    decision = submit(env, proposal(), "edit", edits=LINK)
    env.sink.fail = failure
    with pytest.raises(RuntimeError):
        env.handler.apply(decision)
    assert not env.store.is_applied(decision.proposal_id, 1)
    entries = env.fabric.entries(EntryQuery())
    # Fresh journal and feedback handles: deduplication is durable, not in memory.
    env.handler.store = ReviewStore(env.store.path)
    env.sink.store = SqliteFeedbackStore(env.feedback.path, env.handler.store)
    assert not env.courier.scan_once().problems
    assert env.store.is_applied(decision.proposal_id, 1)
    assert env.fabric.entries(EntryQuery()) == entries and len(entries) == 2
    assert env.sink.store.events() == [(decision.proposal_id, 1)]
    assert [e.entity_id for e in env.sink.store.index().entities] == sorted(
        [KIM, PROJECT]
    )


def test_explicit_choice_adds_alias_and_feeds_extractor(env):
    env.handler.apply(
        submit(env, proposal(name="김민수", aliases=["김 과장"]), "edit", edits=LINK)
    )
    index = env.feedback.index()
    kim = next(e for e in index.entities if e.entity_id == KIM)
    assert (kim.name, kim.aliases) == ("김민수", ["김 과장"])
    # Approved without correction: the AI-linked project gains no alias.
    assert next(e for e in index.entities if e.entity_id == PROJECT).aliases == []
    # The extractor still treats an alias-only mention as a question.
    text = "김 과장이 내일 회의를 요청했다."
    data = example("Proposal")
    reply = {k: data[k] for k in ("summary", "entities", "schedules", "relations")}
    reply["thread_excerpt"] = text
    reply["entry_drafts"] = []
    reply["schedules"] = []
    reply["relations"] = []
    reply["entities"] = [
        data["entities"][0]
        | {"thread_excerpt": text, "ask": None, "entity_id": KIM, "name": "김 과장"}
    ]

    class Fake:
        def generate_json(self, task, system, user, schema):
            self.user = json.loads(user)
            return schema.model_validate(reply)

    fake = Fake()
    thread = Thread(
        schema_version=1,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA9",
        channel="share_text",
        occurred_at=None,
        text=text,
        segments=[],
    )
    from weaver.config import RefinerConfig
    from weaver.loom.refine import RuleRefiner

    candidates = RuleRefiner(RefinerConfig()).select(thread)
    assert candidates
    extraction = LlmExtractor(fake).extract(thread, candidates, index)
    assert [e["entity_id"] for e in fake.user["known"]["entities"]] == [KIM]
    assert extraction.entities[0].entity_id is None
    assert [c.entity_id for c in extraction.entities[0].ask.choices] == [KIM]


def test_conflicting_alias_homonym_and_reject_never_merge(env):
    # Existing entity OTHER already answers to "김 과장" via a new approval.
    first = proposal(
        "prp_01ARZ3NDEKTSV4RRFFQ69G5FB0",
        ask=None,
        entity_id=None,
        name="김철수",
        aliases=["김 과장"],
    )
    env.handler.apply(submit(env, first))
    # Homonym: another new person also named 김철수 becomes a separate entity.
    second = proposal(
        "prp_01ARZ3NDEKTSV4RRFFQ69G5FB1",
        ask=None,
        entity_id=None,
        name="김철수",
        aliases=[],
    )
    env.handler.apply(submit(env, second))
    people = [e for e in env.feedback.index().entities if e.type == "person"]
    assert len(people) == 2 and {p.name for p in people} == {"김철수"}
    # Explicit link of a "김 과장" mention to KIM: alias collides -> conflict only.
    env.handler.apply(
        submit(
            env,
            proposal(
                "prp_01ARZ3NDEKTSV4RRFFQ69G5FB2", name="김민수", aliases=["김 과장"]
            ),
            "edit",
            edits=LINK,
        )
    )
    kim = next(e for e in env.feedback.index().entities if e.entity_id == KIM)
    assert kim.aliases == []
    assert env.feedback.alias_conflicts() == [(KIM, "김 과장")]
    # Reject (even wrong_person) changes nothing.
    before = env.feedback.index()
    env.handler.apply(submit(env, proposal("prp_01ARZ3NDEKTSV4RRFFQ69G5FB3"), "reject"))
    assert env.feedback.index() == before
    assert len({e.entity_id for e in before.entities}) == len(before.entities)


def test_examples_are_structured_related_capped_and_tracked(env):
    settings = FeedbackSettings(example_limit=3, example_bytes=2048)
    env.feedback.settings = settings
    for n in range(5):
        pid = f"prp_01ARZ3NDEKTSV4RRFFQ69G5FC{n}"
        decision = submit(
            env,
            proposal(pid),
            "edit",
            edits=[
                *LINK,
                {"path": "/entities/1/type", "value": "topic"},
                {"path": "/entry_drafts/1/value", "value": "비밀 원문 값"},
            ],
            reviewed_at=(
                datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=n)
            ).isoformat(),
        )
        env.handler.apply(decision)
    chosen = env.feedback.select("loom.extract", "src_x", {KIM})
    assert len(chosen) == 3
    text = json.dumps(chosen, ensure_ascii=False)
    assert "비밀" not in text and "내일 점검" not in text and "김민수" not in text
    assert all(
        c["correction"] in {"entities.entity_id", "entities.type"} for c in chosen
    )
    uses = env.feedback.example_uses()
    assert uses[-1]["rule"] == "loom.examples.v1" and len(uses[-1]["selected"]) == 3
    assert uses[-1]["selected"][0].startswith("prp_01ARZ3NDEKTSV4RRFFQ69G5FC4:")
    # Unrelated entity, other task, or a tight byte cap send nothing extra.
    assert env.feedback.select("loom.extract", "src_x", {OTHER}) == []
    assert env.feedback.select("loom.revise", "src_x", {KIM}) == []
    env.feedback.settings = FeedbackSettings(example_bytes=10)
    assert env.feedback.select("loom.extract", "src_x", {KIM}) == []


def test_extractor_adds_corrections_only_for_selected_entities(env):
    env.handler.apply(submit(env, proposal(), "edit", edits=LINK))
    calls = []

    class Fake:
        model = "fake"

        def generate_json(self, task, system, user, schema):
            calls.append(json.loads(user))
            raise RuntimeError("stop after payload")

    extractor = LlmExtractor(Fake(), examples=env.feedback)
    assert extractor.versions == {
        "prompt": "loom.extract.v1",
        "model": "fake",
        "examples": "loom.examples.v1",
    }
    from weaver.config import RefinerConfig
    from weaver.loom.refine import RuleRefiner

    for text, expected in (
        ("김민수님이 내일 회의를 요청했다.", True),
        ("박영희님이 내일 회의를 요청했다.", False),
    ):
        thread = Thread(
            schema_version=1,
            source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA9",
            channel="share_text",
            occurred_at=None,
            text=text,
            segments=[],
        )
        with pytest.raises(RuntimeError):
            extractor.extract(
                thread,
                RuleRefiner(RefinerConfig()).select(thread),
                env.feedback.index(),
            )
        assert ("corrections" in calls[-1]) is expected


def test_purge_drops_raw_fragments_and_keeps_structure(env):
    decision = submit(env, proposal(), "revise")
    env.handler.apply(decision)
    assert env.feedback.purge(datetime.now(UTC) + timedelta(days=179)) == 0
    assert env.feedback.purge(datetime.now(UTC) + timedelta(days=181)) == 1
    record = env.feedback.event(decision.proposal_id, 1)
    assert record["instruction"] is None and record["purged_at"]
    assert record["verdict"] == "revise" and record["result"] == {"next_rev": 2}
    # A retry after purge is still idempotent (fingerprint retained).
    recorded = env.store.proposal(decision.proposal_id, 1)
    env.feedback.record(
        FeedbackEvent(
            (decision.proposal_id, 1),
            recorded,
            decision,
            env.store.proposal(decision.proposal_id, 2),
            {},
            env.store.versions(decision.proposal_id, 1),
        )
    )


def test_eval_candidate_lifecycle_requires_fake_sample(env, tmp_path):
    decision = submit(
        env,
        proposal(),
        "edit",
        edits=[*LINK, {"path": "/entry_drafts/1/value", "value": "실제 값"}],
    )
    env.handler.apply(decision)
    root = tmp_path / "state" / "eval_candidates"
    evals = tmp_path / "repo" / "server" / "evals"
    [cid] = ec.export_candidates(env.feedback, root)
    assert ec.export_candidates(env.feedback, root) == []
    assert root.stat().st_mode & 0o777 == 0o700
    path = root / f"{cid}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text()
    assert "실제 값" not in raw and "내일 점검" not in raw and KIM not in raw
    candidate = json.loads(raw)
    assert candidate["status"] == "candidate"
    assert candidate["expected"]["entry_kinds"] == ["요청", "일정"]
    assert candidate["versions"]["prompt"] == "loom.extract.v1"
    with pytest.raises(ec.CandidateError):
        ec.promote(env.feedback, root, cid, evals)
    with pytest.raises(ec.CandidateError):
        ec.mark_reviewed(root, cid)
    leaked = tmp_path / "leaked.json"
    leaked.write_text(
        json.dumps({"text": "어제 내일 점검 회의를 요청합니다."}, ensure_ascii=False)
    )
    with pytest.raises(ec.CandidateError, match="original text"):
        ec.mark_authored(env.feedback, root, cid, leaked)
    fake = tmp_path / "fake.json"
    fake.write_text(
        json.dumps(
            {"text": "가상인물 갑이 다음 주 회의를 잡자고 했다."}, ensure_ascii=False
        )
    )
    ec.mark_authored(env.feedback, root, cid, fake)
    with pytest.raises(ec.CandidateError):
        ec.promote(env.feedback, root, cid, evals)
    ec.mark_reviewed(root, cid)
    # Residue is checked again at promotion time.
    fake.write_text(json.dumps({"text": "김민수"}, ensure_ascii=False))
    with pytest.raises(ec.CandidateError):
        ec.promote(env.feedback, root, cid, evals)
    assert not evals.exists() or not list(evals.iterdir())
    fake.write_text(json.dumps({"text": "가상인물 갑"}, ensure_ascii=False))
    target = ec.promote(env.feedback, root, cid, evals)
    published = json.loads(target.read_text())
    assert (
        published["origin"] == "feedback"
        and decision.proposal_id not in target.read_text()
    )
    assert ec.load(root, cid)["status"] == "promoted"
    with pytest.raises(ec.CandidateError):
        ec.promote(env.feedback, root, cid, evals)


def test_proposer_records_extractor_versions(env):
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
    built = Proposer(env.store, env.publisher).build(
        source, extraction, {"prompt": "loom.extract.v1", "model": "x"}
    )
    assert env.store.versions(built.proposal_id, 1) == {
        "prompt": "loom.extract.v1",
        "model": "x",
    }


def test_journal_v2_migrates_to_versions_column(tmp_path):
    import sqlite3

    path = tmp_path / "loom.sqlite3"
    ReviewStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {r[1] for r in db.execute("PRAGMA table_info(proposals)")}
    assert "versions" in columns
    ReviewStore(path)  # reopen is a no-op
