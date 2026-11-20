"""Human-generated lineage over temporary stores, without user data or models."""

import pytest

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.llm.ledger import Ledger
from weaver.llm.policy import LedgerBacklog, PolicyBlocked, Provenance, provenance_scope
from weaver.llm.policy_archive import configured_policy

from .fabric.test_store import draft
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
def test_human_source_without_archive_index(tmp_path, snapshot):
    from weaver.config import load_config

    from .test_config import write_config

    config = load_config(write_config(tmp_path), environ={})
    source_id = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    FabricStore.from_config(config).append(
        [draft("wiki:synthetic", source_id=source_id)]
    )
    assert not (config.archive_root / "index.sqlite3").exists()
    with provenance_scope([source_id], shared=True):
        assert (
            configured_policy(config, snapshot=snapshot).prepare("s", "u").user == "u"
        )
    assert not (config.archive_root / "index.sqlite3").exists()
