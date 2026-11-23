import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

from weaver.context import ContextDictionary
from weaver.context.proposals import LocationProposals, nearest_parent
from weaver.context.tree import expand

NOW = datetime(2026, 11, 20, tzinfo=UTC)
SOURCE_A = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
SOURCE_B = "src_01ARZ3NDEKTSV4RRFFQ69G5FAW"


def propose(store, thread, source=SOURCE_A, *, path="회사/infra/storage/nas", now=NOW):
    return store.propose(
        kind="location",
        reason="unknown_path",
        sphere="회사",
        path=path,
        fallback="회사/미분류",
        thread_ids=[thread],
        source_ids=[source],
        now=now,
    )


def test_dedupe_merge_and_rejected_never_again(tmp_path):
    store = LocationProposals(tmp_path)
    first = propose(store, "t1")
    second = propose(store, "t2", SOURCE_B, now=NOW + timedelta(hours=1))
    assert first.proposal_id == second.proposal_id
    assert second.count == 2 and second.thread_ids == ["t1", "t2"]
    assert second.source_ids == [SOURCE_A, SOURCE_B]
    assert second.created_at == NOW
    assert len(store.pending()) == 1
    # Replaying the same evidence changes nothing.
    assert propose(store, "t2", SOURCE_B).count == 2
    decided = store.decide(first.proposal_id, "rejected", NOW)
    assert decided.status == "rejected"
    assert propose(store, "t3") is None
    assert store.pending() == []
    assert store.get(first.proposal_id).count == 2


def test_pending_is_newest_first_and_decisions_are_final(tmp_path):
    store = LocationProposals(tmp_path)
    old = propose(store, "t1", path="회사/infra/a")
    new = propose(store, "t2", path="회사/infra/b", now=NOW + timedelta(days=1))
    assert [p.proposal_id for p in store.pending()] == [
        new.proposal_id,
        old.proposal_id,
    ]
    accepted = store.decide(old.proposal_id, "accepted", NOW)
    assert store.decide(old.proposal_id, "accepted", NOW) == accepted
    with pytest.raises(ValueError):
        store.decide(old.proposal_id, "rejected", NOW)
    with pytest.raises(KeyError):
        store.decide("prp_" + "0" * 32, "accepted", NOW)
    assert [p.proposal_id for p in store.pending()] == [new.proposal_id]


def test_unclassified_fingerprint_uses_reason_and_key(tmp_path):
    store = LocationProposals(tmp_path)

    def call(key):
        return store.propose(
            kind="unclassified",
            reason="reextract_unclassified",
            sphere="회사",
            path=None,
            fallback="회사/미분류",
            thread_ids=["t1"],
            source_ids=[SOURCE_A],
            now=NOW,
            key=key,
        )

    assert call("plan-1").proposal_id == call("plan-1").proposal_id
    assert call("plan-2").proposal_id != call("plan-1").proposal_id


def test_store_is_private_and_persists(tmp_path):
    store = LocationProposals(tmp_path)
    propose(store, "t1")
    assert stat.S_IMODE(os.stat(tmp_path / "proposals.sqlite3").st_mode) == 0o600
    assert len(LocationProposals(tmp_path).pending()) == 1


def test_nearest_parent_walks_up_to_an_existing_tree_node(tmp_path):
    context = ContextDictionary(tmp_path)
    context.sync_tree("회사", expand("회사", ["회사/infra/network/router"]))
    snapshot = context.snapshot()
    assert nearest_parent(snapshot, "회사/infra/storage/nas") == "회사/infra"
    assert nearest_parent(snapshot, "회사/infra/network/switch") == "회사/infra/network"
    assert nearest_parent(snapshot, "회사") is None
