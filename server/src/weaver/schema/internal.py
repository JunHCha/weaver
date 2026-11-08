"""Server-only boundaries. Never export these as Courier files or phone schemas.

Extraction is the generate_json schema: no server-generated proposal IDs, version,
revision or archive metadata. Conversion injects that context then validates the
same Proposal contract as inbound files. Candidate and EntityIndex are inputs to
Refiner/Extractor; ArchivedSource keeps the local filesystem outside Source JSON.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from .models import (
    Contract,
    Entity,
    EntityId,
    EntityRef,
    EntityType,
    Entry,
    EntryDraft,
    Evidence,
    NonEmpty,
    Proposal,
    Ref,
    RelationType,
    Schedule,
    Source,
    SourceId,
    Thread,
    Timestamp,
)


class ArchivedSource(Contract):
    source: Source
    payload_path: Path


class CandidateTrigger(Contract):
    """Rule hit in original Thread coordinates, with its unmodified time offset."""

    kind: Literal["date", "time", "duration", "honorific"]
    start_char: int = Field(ge=0, strict=True)
    end_char: int = Field(ge=1, strict=True)
    sentence_index: int = Field(ge=0, strict=True)
    segment_ids: list[Ref]
    reference_at: Timestamp | None


class Candidate(Contract):
    """A selected Thread range; offsets index original Unicode code points.

    The 0..1 sentence defaults preserve callers of the initial v1 internal
    Candidate, which supplied only char ranges and context. They are legacy
    placeholders, not inferred sentence coordinates. RuleRefiner always supplies
    actual sentence bounds with its triggers; new producers should do the same.
    """

    source_id: SourceId
    segment_ids: list[Ref]
    start_char: int = Field(ge=0, strict=True)
    end_char: int = Field(ge=1, strict=True)
    context: NonEmpty
    start_sentence: int = Field(default=0, ge=0, strict=True)
    end_sentence: int = Field(default=1, ge=1, strict=True)
    triggers: list[CandidateTrigger] = Field(default_factory=list)

    def validate_thread(self, thread: Thread) -> None:
        if self.source_id != thread.source_id:
            raise ValueError("candidate source does not match Thread")
        if not 0 <= self.start_char < self.end_char <= len(thread.text):
            raise ValueError("candidate range exceeds Thread")
        if not set(self.segment_ids) <= {s.segment_id for s in thread.segments}:
            raise ValueError("unknown candidate segment")
        if self.context != thread.text[self.start_char : self.end_char]:
            raise ValueError("candidate context does not match Thread")
        if self.end_sentence <= self.start_sentence:
            raise ValueError("candidate sentence range must be ordered")
        for trigger in self.triggers:
            if (
                not self.start_char
                <= trigger.start_char
                < trigger.end_char
                <= self.end_char
            ):
                raise ValueError("trigger exceeds candidate range")
            if not self.start_sentence <= trigger.sentence_index < self.end_sentence:
                raise ValueError("trigger exceeds candidate sentence range")
            if not set(trigger.segment_ids) <= set(self.segment_ids):
                raise ValueError("unknown trigger segment")


class IndexedEntity(Contract):
    entity_id: EntityId
    type: EntityType
    name: NonEmpty
    aliases: list[NonEmpty]


class EntityIndex(Contract):
    """Caller-owned approved Fabric entity/alias snapshot; extraction reads only."""

    entities: list[IndexedEntity]


class ExtractedEntity(Evidence):
    ref: Ref
    entity_id: EntityId | None
    type: EntityType
    name: NonEmpty
    aliases: list[NonEmpty]


class ExtractedRelation(Evidence):
    """LLM relation uses the same fields except the server-owned version."""

    from_ref: EntityRef | Literal["this"] = Field(alias="from")
    type: RelationType
    to: EntityRef | Literal["this"]
    reason: NonEmpty | None


class Extraction(Contract):
    """Validated extraction boundary, not a provider-portable structured schema.

    loom.extract.ExtractionOutput simplifies provider-incompatible constraints.
    LlmExtractor revalidates this full contract and Proposal reference integrity.
    """

    summary: NonEmpty
    thread_excerpt: NonEmpty
    entities: list[ExtractedEntity]
    schedules: list[Schedule]
    relations: list[ExtractedRelation]
    entry_drafts: list[EntryDraft]

    def to_proposal(self, source: Source, proposal_id: str, rev: int) -> Proposal:
        payload = self.model_dump(mode="json", by_alias=True)
        payload.update(
            schema_version=1,
            proposal_id=proposal_id,
            rev=rev,
            source=source.model_dump(
                mode="json",
                include={"source_id", "channel", "captured_at", "occurred_at"},
            ),
        )
        return Proposal.model_validate(payload)


def materialize_entries(
    proposal: Proposal,
    *,
    entity_ids: dict[str, str],
    entry_ids: dict[str, str],
    recorded_at: datetime,
) -> list[Entry]:
    """Map approved draft refs to durable IDs; return facts without executing them.

    Caller allocates new ent_/evt_ ULIDs once, preserves existing entity matches,
    and supplies recorded_at. All generated entries are ai_approved facts. Request
    blocks persist as evt_ references. Fabric must verify resolves targets a request
    and decide execution eligibility; proposal approval alone is insufficient.
    """
    if set(entity_ids) != {e.ref for e in proposal.entities}:
        raise ValueError("entity_ids must cover exactly the proposal entity refs")
    if set(entry_ids) != {d.ref for d in proposal.entry_drafts}:
        raise ValueError("entry_ids must cover exactly the draft refs")
    if len(set(entry_ids.values())) != len(entry_ids):
        raise ValueError("drafts require distinct Entry IDs")
    existing = {e.entity_id for e in proposal.entities if e.entity_id is not None}
    allocated = [entity_ids[e.ref] for e in proposal.entities if e.entity_id is None]
    if len(allocated) != len(set(allocated)) or existing.intersection(allocated):
        raise ValueError(
            "new entities require distinct IDs disjoint from existing matches"
        )
    for entity in proposal.entities:
        if entity.entity_id is not None and entity_ids[entity.ref] != entity.entity_id:
            raise ValueError("cannot replace an existing entity match")
        # Validate IDs even when there are no drafts that reference the entity.
        Entity.model_validate(
            entity.model_dump(mode="json")
            | {"schema_version": 1, "entity_id": entity_ids[entity.ref]}
        )

    def resolve_entity(ref: str) -> str:
        return entity_ids.get(ref, ref)

    entries = []
    for draft in proposal.entry_drafts:
        payload = draft.model_dump(
            mode="json", exclude={"ref", "confidence", "ask", "thread_excerpt"}
        )
        payload.update(
            schema_version=1,
            entry_id=entry_ids[draft.ref],
            entities=[resolve_entity(ref) for ref in draft.entities],
            source_id=proposal.source.source_id,
            recorded_at=recorded_at,
            actor="ai_approved",
            resolves=None,
            blocked_by_request=entry_ids[draft.blocked_by_request]
            if draft.blocked_by_request is not None
            else None,
        )
        if draft.request is not None:
            payload["request"] = draft.request.model_dump(mode="json") | {
                "requester": resolve_entity(draft.request.requester),
                "targets": [resolve_entity(ref) for ref in draft.request.targets],
            }
        entries.append(Entry.model_validate(payload))
    return entries


def create_request_decision(
    request: Entry,
    *,
    entry_id: str,
    value: JsonValue,
    occurred_at: datetime,
    recorded_at: datetime,
    source_id: str,
) -> Entry:
    """Record an explicit user decision outside the AI Proposal approval path.

    Call only in response to a user decision; this factory never executes actions.
    Fabric still owns state folding, including retraction and decision semantics.
    """
    if request.kind != "요청" or request.request is None:
        raise ValueError("decision target must be a request Entry")
    return Entry.model_validate(
        {
            "schema_version": 1,
            "entry_id": entry_id,
            "entities": request.entities,
            "kind": "결정",
            "field": None,
            "value": value,
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "source_id": source_id,
            "actor": "human",
            "supersedes": None,
            "retracts": None,
            "request": None,
            "resolves": request.entry_id,
            "blocked_by_request": None,
        }
    )
