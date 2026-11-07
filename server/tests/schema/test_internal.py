"""Extraction and materialization boundaries use only synthetic fixture data."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from weaver.schema import Proposal, Source, Thread
from weaver.schema.internal import Candidate, Extraction, materialize_entries

from .test_contract import example


def extraction_payload():
    payload = example("Proposal")
    for key in ["schema_version", "proposal_id", "rev", "source"]:
        del payload[key]
    for entity in payload["entities"]:
        del entity["schema_version"]
    for relation in payload["relations"]:
        del relation["schema_version"]
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
        actor="ai_approved",
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


def test_human_decision_maps_request_ref_and_ai_approval_cannot_resolve():
    payload = example("Proposal")
    payload["schedules"] = []
    payload["entry_drafts"][1].update(
        kind="결정", resolves="d1", blocked_by_request=None
    )
    proposal = Proposal.model_validate(payload)
    with pytest.raises(ValidationError):
        materialize_entries(proposal, **mapping())
    entries = materialize_entries(proposal, **(mapping() | {"actor": "human"}))
    assert entries[1].resolves == entries[0].entry_id


def test_candidate_keeps_thread_and_segment_provenance():
    thread = Thread.model_validate(example("Thread"))
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
