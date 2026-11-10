"""Extraction and materialization boundaries use only synthetic fixture data."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from weaver.schema import Proposal, Script, Source
from weaver.schema.internal import (
    Candidate,
    Extraction,
    create_request_decision,
    materialize_entries,
)

from .test_contract import example


def extraction_payload():
    payload = example("Proposal")
    for key in ["schema_version", "proposal_id", "rev", "source"]:
        del payload[key]
    return payload


def test_extraction_is_distinct_and_converts_through_shared_validation():
    payload = extraction_payload()
    extraction = Extraction.model_validate(payload)
    source = Source.model_validate(example("Source"))
    original = example("Proposal")
    proposal = extraction.to_proposal(source, original["proposal_id"], 1)
    assert proposal == Proposal.model_validate(original)
    assert "proposal_id" not in Extraction.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        Extraction.model_validate(example("Proposal"))
    with pytest.raises(ValidationError):
        extraction.to_proposal(source, "invalid", 1)
    with pytest.raises(ValidationError):
        extraction.to_proposal(source, original["proposal_id"], 0)
    payload["entry_drafts"][0]["entities"] = ["e9"]
    extraction = Extraction.model_validate(payload)
    with pytest.raises(ValidationError):
        extraction.to_proposal(source, original["proposal_id"], 1)


def mapping():
    suffix = "01ARZ3NDEKTSV4RRFFQ69G5FA"
    return dict(
        entity_ids={"e1": f"ent_{suffix}0", "e2": f"ent_{suffix}1"},
        entry_ids={"d1": f"evt_{suffix}0", "d2": f"evt_{suffix}1"},
        recorded_at=datetime.fromisoformat("2026-11-07T16:00:00+09:00"),
    )


def test_draft_mapping_preserves_request_and_execution_block():
    entries = materialize_entries(
        Proposal.model_validate(example("Proposal")), **mapping()
    )
    request, schedule = entries
    assert request.kind == "요청"
    assert request.request.requester == request.entities[0]
    assert request.request.targets == [request.entities[1]]
    assert request.resolves is None
    assert schedule.blocked_by_request == request.entry_id
    assert all(entry.actor == "ai_approved" for entry in entries)
    assert schedule.source_id == request.source_id == example("Source")["source_id"]


@pytest.mark.parametrize(
    "invalid", ["matched_id", "missing_id", "duplicate_id", "bad_id"]
)
def test_mapping_rejects_invalid_allocations(invalid):
    args = mapping()
    if invalid == "matched_id":
        args["entity_ids"]["e2"] = args["entity_ids"]["e1"]
    elif invalid == "missing_id":
        del args["entry_ids"]["d2"]
    elif invalid == "duplicate_id":
        args["entry_ids"]["d2"] = args["entry_ids"]["d1"]
    else:
        args["entity_ids"]["e1"] = "ent_bad"
    with pytest.raises(ValueError):
        materialize_entries(Proposal.model_validate(example("Proposal")), **args)


@pytest.mark.parametrize("resolves", ["d1", "evt_01ARZ3NDEKTSV4RRFFQ69G5FA0"])
def test_ai_drafts_cannot_resolve_requests(resolves):
    payload = example("Proposal")
    payload["schedules"] = []
    payload["entry_drafts"][1].update(
        kind="결정", resolves=resolves, blocked_by_request=None
    )
    with pytest.raises(ValidationError):
        Proposal.model_validate(payload)
    payload = extraction_payload()
    payload["entry_drafts"][1].update(kind="결정", resolves=resolves)
    with pytest.raises(ValidationError):
        Extraction.model_validate(payload)


def test_materialization_has_no_human_actor_override():
    proposal = Proposal.model_validate(example("Proposal"))
    with pytest.raises(TypeError):
        materialize_entries(proposal, **mapping(), actor="human")


def test_explicit_human_decision_is_separate_from_proposal():
    entries = materialize_entries(
        Proposal.model_validate(example("Proposal")), **mapping()
    )
    args = dict(
        entry_id="evt_01ARZ3NDEKTSV4RRFFQ69G5FA2",
        value="진행 승인",
        occurred_at=mapping()["recorded_at"],
        recorded_at=mapping()["recorded_at"],
        source_id="output:review/request",
    )
    decision = create_request_decision(entries[0], **args)
    assert decision.actor == "human"
    assert decision.resolves == entries[0].entry_id
    assert all(e.actor == "ai_approved" and e.resolves is None for e in entries)
    with pytest.raises(ValueError):
        create_request_decision(entries[1], **args)


@pytest.mark.parametrize("collision", ["new_new", "new_existing"])
def test_new_entity_allocations_cannot_silently_merge(collision):
    payload = example("Proposal")
    args = mapping()
    if collision == "new_new":
        payload["entities"][1]["entity_id"] = None
    args["entity_ids"]["e1"] = args["entity_ids"]["e2"]
    with pytest.raises(ValueError, match="distinct IDs"):
        materialize_entries(Proposal.model_validate(payload), **args)


def test_explicit_existing_matches_can_share_an_id():
    payload = example("Proposal")
    payload["entities"][0]["entity_id"] = payload["entities"][1]["entity_id"]
    args = mapping()
    args["entity_ids"]["e1"] = args["entity_ids"]["e2"]
    entries = materialize_entries(Proposal.model_validate(payload), **args)
    assert entries[0].entities[0] == entries[0].entities[1]


def test_candidate_keeps_thread_and_segment_provenance():
    thread = Script.model_validate(example("Script"))
    candidate = Candidate(
        source_id=thread.source_id,
        segment_ids=["t1"],
        start_char=0,
        end_char=len(thread.text),
        context=thread.text,
    )
    candidate.validate_thread(thread)
    candidate.end_char += 1
    with pytest.raises(ValueError):
        candidate.validate_thread(thread)
