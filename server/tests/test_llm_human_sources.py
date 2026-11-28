"""Human-generated lineage over temporary stores, without user data or models."""

import sqlite3
from contextlib import closing

import pytest

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.llm.ledger import Ledger
from weaver.llm.policy import LedgerBacklog, PolicyBlocked, Provenance, provenance_scope
from weaver.llm.policy_archive import configured_policy

from .spec.test_facts_and_history import draft
from .test_llm_policy_gate import archive_webex, make_vault


def wiki_observation(config, fabric):
    from weaver.wiki.compiler import Compiler, EditChange, EditResult, Summary
    from weaver.wiki.store import HumanEdit, WikiStore

    from .fake_responses import fixture_response
    from .wiki.test_compiler import FakeLLM

    class Fake(FakeLLM):
        def generate_json(self, task, system, user, schema):
            return fixture_response(
                EditResult,
                {
                    "changes": [
                        EditChange(content="합성 사람 편집", tags=["합성 프로젝트"])
                    ]
                },
            )

    store = WikiStore(config.vault_root, config.state_root, settle_seconds=0)
    compiler = Compiler(
        store,
        fabric,
        ContextDictionary(config.state_root),
        Fake(),
        config.state_root / "compiler.sqlite3",
        lambda _: Summary("합성 요약", "합성 본문"),
    )
    store.acknowledge = lambda edit: None
    compiler._edit(HumanEdit("pages/합성.md", "old", "new", "합성 편집", {}, {}))
    return next(t for t in fabric.entries(ThreadQuery()) if t.actor == "human")


def mobile_request(config, fabric, source_id):
    from weaver.mobile.requests import RequestEdit, RequestEditService
    from weaver.mobile.store import MobileStore

    target = fabric.append(
        [draft("request", source_id=source_id, kind="요청", direction="받음")]
    )[0]
    store = MobileStore(config.state_root)
    device, _ = store.create_device("synthetic", "node", frozenset({"edit"}))
    RequestEditService(config, store).apply(
        device.device_id,
        "complete",
        RequestEdit(
            client_id="synthetic-complete",
            thread_id=target.thread_id,
            expected_revision=target.seq,
            content="합성 요청 완료",
        ),
    )
    return next(
        t
        for t in fabric.entries(ThreadQuery())
        if t.thread_id.startswith("phone-request:")
    )


@pytest.fixture(params=["wiki", "mobile"])
def human_source(tmp_path, request):
    config, source_id, _ = make_vault(tmp_path, webex=False)
    fabric = FabricStore.from_config(config)
    row = (
        wiki_observation(config, fabric)
        if request.param == "wiki"
        else mobile_request(config, fabric, source_id)
    )
    assert row.actor == "human"
    return config, fabric, row.source_id


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("webex", [False, True])
def test_human_synthetic_lineage_uses_unknown_wiki_rule(human_source, snapshot, webex):
    config, _, source_id = human_source
    if webex:
        archive_webex(config, 1, "SPACE_OPEN")
    policy = configured_policy(config, snapshot=snapshot)
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy.backlog = LedgerBacklog(ledger)
    assert source_id in policy.shared_sources()
    # Both direct Wiki lineage and shared maintenance must apply the same rule.
    if webex:
        with pytest.raises(PolicyBlocked):
            policy.check(Provenance(frozenset({source_id}), requester=source_id))
        with provenance_scope([source_id], shared=True, requester=source_id):
            with pytest.raises(PolicyBlocked):
                policy.prepare("s", "u")
    else:
        policy.check(Provenance(frozenset({source_id}), requester=source_id))
        with provenance_scope([source_id], shared=True, requester=source_id):
            assert policy.prepare("s", "u").user == "u"
    assert ledger.policy_backlog() == []


@pytest.mark.parametrize("actor", ["ai_approved", "system"])
def test_nonhuman_reference_keeps_missing_source_closed(human_source, actor):
    config, fabric, source_id = human_source
    policy = configured_policy(config)
    policy.check(Provenance(frozenset({source_id})))
    target = next(t for t in fabric.entries(ThreadQuery()) if t.source_id == source_id)
    fabric.append(
        [
            draft(
                "nonhuman",
                actor=actor,
                source_id=source_id,
                retracts=target.thread_id if actor == "system" else None,
            )
        ]
    )
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy.backlog = LedgerBacklog(ledger)
    with provenance_scope([source_id], shared=True), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")
    assert [sid for sid, _ in ledger.policy_backlog()] == [source_id]


def test_archived_origin_wins_over_human_actor(human_source):
    config, fabric, _ = human_source
    source_id = archive_webex(config, 1, "SPACE_PRIVATE")
    fabric.append([draft("archived", actor="human", source_id=source_id)])
    policy = configured_policy(config)
    policy.excluded_space_ids = frozenset({"SPACE_PRIVATE"})
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy.backlog = LedgerBacklog(ledger)
    with provenance_scope([source_id]), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")
    assert [sid for sid, _ in ledger.policy_backlog()] == [source_id]


def test_unreferenced_missing_source_stays_closed(human_source):
    config, _, _ = human_source
    policy = configured_policy(config)
    with provenance_scope(["src_01ARZ3NDEKTSV4RRFFQ69G5FAV"]):
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")


def test_blocked_human_lineage_records_only_archived_requester(human_source):
    config, _, source_id = human_source
    requester = archive_webex(config, 1, "SPACE_OPEN")
    policy = configured_policy(config)
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy.backlog = LedgerBacklog(ledger)
    with provenance_scope([source_id], shared=True, requester=requester):
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")
    assert [sid for sid, _ in ledger.policy_backlog()] == [requester]


@pytest.mark.parametrize("snapshot", [False, True])
def test_human_source_without_archive_index_fails_closed(tmp_path, snapshot):
    from weaver.config import load_config

    from .test_config import write_config

    config = load_config(write_config(tmp_path), environ={})
    source_id = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    FabricStore.from_config(config).append(
        [draft("wiki:synthetic", source_id=source_id)]
    )
    assert not (config.archive_root / "index.sqlite3").exists()
    policy = configured_policy(config, snapshot=snapshot)
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy.backlog = LedgerBacklog(ledger)
    with provenance_scope([source_id], shared=True):
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")
    # Even an existing unknown-lineage sentinel must not infer no Webex from
    # a missing index, independently of normalization.
    from weaver.llm.policy import UNKNOWN_WIKI_SOURCE, LlmPolicy
    from weaver.llm.policy_archive import ArchiveProvenanceResolver

    probe = LlmPolicy(
        webex_present=ArchiveProvenanceResolver(
            config.archive_root, snapshot=snapshot
        ).has_webex_source,
        backlog=LedgerBacklog(ledger),
    )
    with pytest.raises(PolicyBlocked):
        probe.check(Provenance(frozenset({UNKNOWN_WIKI_SOURCE})))
    assert ledger.policy_backlog() == []
    assert not (config.archive_root / "index.sqlite3").exists()


@pytest.mark.parametrize("snapshot", [False, True])
def test_normalization_reads_each_id_set_once(tmp_path, monkeypatch, snapshot):
    config, _, _ = make_vault(tmp_path, webex=False)
    ids = frozenset(f"src_{i:026d}" for i in range(40))
    FabricStore.from_config(config).append(
        [draft(f"wiki:{i}", source_id=sid) for i, sid in enumerate(sorted(ids))]
    )
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    policy = configured_policy(config, snapshot=snapshot)
    for _ in range(2):
        statements.clear()
        policy.check(Provenance(ids, requester=min(ids)))
        assert sum("FROM threads" in sql for sql in statements) == 1
        assert sum("SELECT source_id FROM sources" in sql for sql in statements) == 1


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("fault", ["archive_open", "archive_read", "fabric_open"])
def test_normalization_database_fault_is_transient(
    human_source, monkeypatch, snapshot, fault
):
    config, fabric, source_id = human_source
    index = config.archive_root / "index.sqlite3"
    if fault == "archive_read":
        with closing(sqlite3.connect(index)) as db:
            db.execute("ALTER TABLE sources RENAME TO unavailable_sources")
            db.commit()
    else:
        connect = sqlite3.connect
        broken = index if fault == "archive_open" else fabric.path

        def fail_open(database, *args, **kwargs):
            if str(database).startswith(broken.resolve().as_uri()):
                raise sqlite3.OperationalError("synthetic private DB error")
            return connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", fail_open)
    policy = configured_policy(config, snapshot=snapshot)
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy.backlog = LedgerBacklog(ledger)
    with pytest.raises(PolicyBlocked, match="^policy_blocked$"):
        policy.check(Provenance(frozenset({source_id}), requester=source_id))
    if fault.startswith("archive_"):
        from weaver.llm.policy import UNKNOWN_WIKI_SOURCE, LlmPolicy
        from weaver.llm.policy_archive import ArchiveProvenanceResolver

        probe = LlmPolicy(
            webex_present=ArchiveProvenanceResolver(
                config.archive_root, snapshot=snapshot
            ).has_webex_source,
            backlog=LedgerBacklog(ledger),
        )
        with pytest.raises(PolicyBlocked, match="^policy_blocked$"):
            probe.check(Provenance(frozenset({UNKNOWN_WIKI_SOURCE})))
    assert ledger.policy_backlog() == []
    if fault == "archive_read":
        with closing(sqlite3.connect(index)) as db:
            db.execute("ALTER TABLE unavailable_sources RENAME TO sources")
            db.commit()
    else:
        monkeypatch.setattr(sqlite3, "connect", connect)
    # The same policy retries the read instead of leaving a permanent hold.
    policy.check(Provenance(frozenset({source_id}), requester=source_id))


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize(
    "payload", ["not json", '{"source_id":"other","actor":"human"}']
)
def test_unrelated_corrupt_fabric_payload_fails_closed(human_source, snapshot, payload):
    config, fabric, source_id = human_source
    # FabricStore creates the real format; inject corruption only in this
    # temporary DB to exercise the reader's failure path.
    with closing(sqlite3.connect(fabric.path)) as db:
        db.execute(
            "INSERT INTO threads(thread_id, payload) VALUES (?, ?)",
            ("corrupt", payload),
        )
        db.commit()
    policy = configured_policy(config, snapshot=snapshot)
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy.backlog = LedgerBacklog(ledger)
    with pytest.raises(PolicyBlocked, match="^policy_blocked$"):
        policy.check(Provenance(frozenset({source_id}), requester=source_id))
    assert ledger.policy_backlog() == []


@pytest.mark.parametrize("database", ["archive", "fabric"])
def test_snapshot_normalization_rejects_active_wal(human_source, database):
    config, fabric, source_id = human_source
    path = (
        config.archive_root / "index.sqlite3" if database == "archive" else fabric.path
    )
    policy = configured_policy(config, snapshot=True)
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy.backlog = LedgerBacklog(ledger)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE snapshot_guard (value TEXT)")
        writer.commit()
        with pytest.raises(PolicyBlocked, match="^policy_blocked$"):
            policy.check(Provenance(frozenset({source_id}), requester=source_id))
        assert ledger.policy_backlog() == []
    policy.check(Provenance(frozenset({source_id}), requester=source_id))
