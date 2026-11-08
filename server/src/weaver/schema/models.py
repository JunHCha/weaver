"""Schema v1 wire contracts; state folding and action execution belong to Fabric.

All keys are required, including nullable keys. Unknown fields and versions fail
closed. IDs use canonical uppercase Crockford ULIDs (first digit at most 7).
"""

import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)

ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
type CaptureId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=rf"^cap_{ULID}$", min_length=30, max_length=30
    ),
]
type SourceId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=rf"^src_{ULID}$", min_length=30, max_length=30
    ),
]
type ProposalId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=rf"^prp_{ULID}$", min_length=30, max_length=30
    ),
]
type AlertId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=rf"^alt_{ULID}$", min_length=30, max_length=30
    ),
]
type EntityId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=rf"^ent_{ULID}$", min_length=30, max_length=30
    ),
]
type EntryId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=rf"^evt_{ULID}$", min_length=30, max_length=30
    ),
]
type Ref = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z][1-9][0-9]*$"),
    Field(json_schema_extra={"not": {"pattern": r"[\r\n]"}}),
]
type EntityRef = EntityId | Ref
type NonEmpty = Annotated[str, StringConstraints(strict=True, pattern=r"\S")]
type Confidence = Annotated[float, Field(ge=0, le=1, strict=True)]
type Revision = Annotated[StrictInt, Field(ge=1)]
type Offset = Annotated[StrictInt, Field(ge=0)]
type Digest = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64
    ),
]
type Channel = Literal["call", "voice_memo", "kakao", "share_text"]
type Kind = Literal["fleece", "text"]
type EntityType = Literal["person", "org", "project", "topic", "meeting"]
type RelationType = Literal["원인", "후속조치", "같은논의", "참석", "소속"]
type EntryKind = Literal["요청", "논의", "결정", "폐기", "약속", "일정", "변경"]
type RejectReason = Literal[
    "wrong_person", "wrong_content", "duplicate", "not_useful", "other"
]


TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def timestamp_input(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        TIMESTAMP_PATTERN,
        value,
    ):
        raise ValueError("expected RFC 3339 timestamp with an explicit timezone")
    return value


type Timestamp = Annotated[
    AwareDatetime,
    BeforeValidator(timestamp_input),
    WithJsonSchema(
        {
            "type": "string",
            "format": "date-time",
            "pattern": TIMESTAMP_PATTERN,
            "not": {"pattern": r"[\r\n]"},
        }
    ),
]
# Logical archive key only. SourceStore resolves it locally; never a machine Path.
type PayloadRef = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*(?:\.[A-Za-z0-9_-]+)?$",
    ),
    Field(json_schema_extra={"not": {"pattern": r"[\r\n]"}}),
]
type Pointer = Annotated[
    str, StringConstraints(strict=True, pattern=r"^(?:/(?:[^~/]|~[01])*)*$")
]
type EntrySource = (
    SourceId
    | Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^output:\S+$"),
        Field(json_schema_extra={"not": {"pattern": r"[\r\n]"}}),
    ]
)


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, serialize_by_alias=True
    )


class Versioned(Contract):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_is_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value


class Hints(Contract):
    counterpart: NonEmpty | None


class Manifest(Versioned):
    capture_id: CaptureId
    kind: Kind
    channel: Channel
    payload: PayloadRef
    sha256: Digest
    size: Offset
    captured_at: Timestamp
    occurred_at: Timestamp | None
    hints: Hints


class Receipt(Versioned):
    capture_id: CaptureId
    source_id: SourceId
    sha256: Digest
    received_at: Timestamp


class Source(Versioned):
    """Archive metadata. payload_ref is a logical relative key, never a local path."""

    source_id: SourceId
    capture_id: CaptureId
    kind: Kind
    channel: Channel
    captured_at: Timestamp
    occurred_at: Timestamp | None
    sha256: Digest
    hints: Hints
    payload_ref: PayloadRef


class Segment(Contract):
    """Text range [start_char,end_char) uses Unicode code points, not UTF-16.

    Segment IDs are unique within a Thread. Audio offsets are integer milliseconds
    from payload start; message occurred_at and speaker preserve Kakao provenance.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"start_ms": {"type": "null"}}},
            "then": {"properties": {"end_ms": {"type": "null"}}},
            "else": {"properties": {"end_ms": {"type": "integer"}}},
        }
    )
    segment_id: Ref
    start_char: Offset
    end_char: Offset
    start_ms: Offset | None
    end_ms: Offset | None
    speaker: NonEmpty | None
    occurred_at: Timestamp | None

    @model_validator(mode="after")
    def valid_ranges(self) -> Self:
        if self.end_char <= self.start_char:
            raise ValueError("text range must be nonempty and ordered")
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("audio offsets must both be null or both be present")
        if self.start_ms is not None and self.end_ms is not None:
            if self.end_ms <= self.start_ms:
                raise ValueError("audio range must be nonempty and ordered")
        return self


class Thread(Versioned):
    """Spinner text and original segment provenance, without archive local paths."""

    source_id: SourceId
    channel: Channel
    occurred_at: Timestamp | None
    text: NonEmpty
    segments: list[Segment]

    @model_validator(mode="after")
    def valid_segments(self) -> Self:
        ids = [s.segment_id for s in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate segment_id")
        for segment in self.segments:
            if segment.end_char > len(self.text):
                raise ValueError("segment exceeds Thread text")
        return self


class Choice(Contract):
    entity_id: EntityId
    label: NonEmpty


class Ask(Contract):
    """A human question; choices may be empty and free-text answers are allowed."""

    question: NonEmpty
    choices: list[Choice]


class Evidence(Contract):
    confidence: Confidence
    ask: Ask | None
    thread_excerpt: NonEmpty


class EntityData(Evidence):
    """ref identifies a proposal item; entity_id links an existing Fabric entity.

    At least one is non-null. aliases never imply identity automatically.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {"properties": {"ref": {"type": "string"}}},
                {"properties": {"entity_id": {"type": "string"}}},
            ]
        }
    )
    ref: Ref | None
    entity_id: EntityId | None
    type: EntityType
    name: NonEmpty
    aliases: list[NonEmpty]

    @model_validator(mode="after")
    def has_identity(self) -> Self:
        if self.ref is None and self.entity_id is None:
            raise ValueError("ref or entity_id is required")
        return self


class Entity(EntityData, Versioned):
    """Standalone versioned entity file; nested entities inherit the file version."""


class ProposalEntity(EntityData):
    """Proposal-local entity with a required non-null ref and no nested version."""

    ref: Ref


class RelationData(Evidence):
    """from/to are entity refs or ent_ IDs; 'this' denotes the proposal source."""

    from_ref: EntityRef | Literal["this"] = Field(alias="from")
    type: RelationType
    to: EntityRef | Literal["this"]
    reason: NonEmpty | None


class Relation(RelationData, Versioned):
    """Standalone versioned relation file; nested relations have no version key."""


class SourceSummary(Contract):
    source_id: SourceId
    channel: Channel
    captured_at: Timestamp
    occurred_at: Timestamp | None


class Request(Contract):
    """A request is data awaiting a user decision, never execution permission."""

    requester: EntityRef
    targets: Annotated[list[EntityRef], Field(min_length=1)]
    due_at: Timestamp | None
    content: NonEmpty


class StoredRequest(Contract):
    requester: EntityId
    targets: Annotated[list[EntityId], Field(min_length=1)]
    due_at: Timestamp | None
    content: NonEmpty


# Express kind-dependent nullability in exported JSON Schema as well as Python.
ENTRY_RULES: list[JsonValue] = [
    {
        "if": {"properties": {"kind": {"const": "요청"}}},
        "then": {"properties": {"request": {"type": "object"}}},
        "else": {"properties": {"request": {"type": "null"}}},
    },
    {
        "if": {"properties": {"kind": {"enum": ["결정"]}}},
        "else": {"properties": {"resolves": {"type": "null"}}},
    },
]
ENTRY_CONDITIONS: dict[str, JsonValue] = {"allOf": ENTRY_RULES}


class EntryDraft(Evidence):
    """Approval records facts only. blocked_by_request forbids derived execution
    until a user-authored decision Entry resolves the referenced request Entry.
    resolves is always null: AI-generated drafts cannot resolve requests.
    """

    model_config = ConfigDict(json_schema_extra=ENTRY_CONDITIONS)
    ref: Ref
    entities: list[EntityRef]
    kind: EntryKind
    field: NonEmpty | None
    value: JsonValue
    occurred_at: Timestamp
    supersedes: EntryId | None
    retracts: EntryId | None
    request: Request | None
    resolves: None
    blocked_by_request: Ref | None

    @model_validator(mode="after")
    def kind_fields(self) -> Self:
        validate_entry_kind(self.kind, self.request, self.resolves)
        return self


class Entry(Versioned):
    """Append-only fact, not action permission. A request is open until a human
    decision Entry has resolves=request.entry_id. Fabric defines state folding,
    replacement/retraction ordering and checks the target of resolves.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                *ENTRY_RULES,
                {
                    "if": {"properties": {"resolves": {"type": "string"}}},
                    "then": {"properties": {"actor": {"const": "human"}}},
                },
            ]
        }
    )
    entry_id: EntryId
    entities: list[EntityId]
    kind: EntryKind
    field: NonEmpty | None
    value: JsonValue
    occurred_at: Timestamp
    recorded_at: Timestamp
    source_id: EntrySource
    actor: Literal["human", "ai_approved"]
    supersedes: EntryId | None
    retracts: EntryId | None
    request: StoredRequest | None
    resolves: EntryId | None
    blocked_by_request: EntryId | None

    @model_validator(mode="after")
    def kind_fields(self) -> Self:
        validate_entry_kind(self.kind, self.request, self.resolves)
        if self.resolves is not None and self.actor != "human":
            raise ValueError("only a human decision can resolve a request")
        return self


def validate_entry_kind(kind: str, request: object, resolves: str | None) -> None:
    if (kind == "요청") != (request is not None):
        raise ValueError("request must be present exactly for kind=요청")
    if resolves is not None and kind != "결정":
        raise ValueError("only kind=결정 can resolve a request")


class Schedule(Evidence):
    """Calendar creation is forbidden while blocked_by_request is non-null,
    until a human decision Entry resolves it. Verdict approve only records facts.
    """

    ref: Ref
    title: NonEmpty
    start: Timestamp
    end: Timestamp | None
    entities: list[EntityRef]
    entry_ref: Ref
    blocked_by_request: Ref | None

    @model_validator(mode="after")
    def ordered_time(self) -> Self:
        if self.end is not None and self.end <= self.start:
            raise ValueError("schedule end must follow start")
        return self


class Proposal(Versioned):
    """One source review unit. Producers must mark request-derived drafts and
    schedules blocked_by_request; approval records the request, never executes it.
    """

    proposal_id: ProposalId
    rev: Revision
    source: SourceSummary
    summary: NonEmpty
    thread_excerpt: NonEmpty
    entities: list[ProposalEntity]
    schedules: list[Schedule]
    relations: list[RelationData]
    entry_drafts: list[EntryDraft]

    @model_validator(mode="after")
    def reference_integrity(self) -> Self:
        validate_proposal_references(
            self.entities, self.schedules, self.relations, self.entry_drafts
        )
        return self


def validate_proposal_references(
    entities: list[ProposalEntity],
    schedules: list[Schedule],
    relations: list[RelationData],
    entry_drafts: list[EntryDraft],
) -> None:
    """Shared integrity check for extraction and wire Proposal boundaries."""
    entity_refs: set[str] = set()
    for entity in entities:
        if entity.ref is None or entity.ref in entity_refs:
            raise ValueError("proposal entities require unique non-null refs")
        entity_refs.add(entity.ref)
    known = entity_refs | {e.entity_id for e in entities if e.entity_id is not None}
    draft_refs = [d.ref for d in entry_drafts]
    schedule_refs = [s.ref for s in schedules]
    all_refs = list(entity_refs) + draft_refs + schedule_refs
    if len(all_refs) != len(set(all_refs)):
        raise ValueError("local refs must be unique across proposal items")
    drafts = {d.ref: d for d in entry_drafts}
    requests = {d.ref for d in entry_drafts if d.kind == "요청"}

    def check_entities(refs: list[str]) -> None:
        if any(ref not in known for ref in refs):
            raise ValueError("dangling entity reference")

    for relation in relations:
        check_entities([r for r in (relation.from_ref, relation.to) if r != "this"])
    for draft in entry_drafts:
        check_entities(draft.entities)
        if draft.request is not None:
            check_entities([draft.request.requester, *draft.request.targets])
        if draft.blocked_by_request is not None:
            if draft.blocked_by_request not in requests or draft.ref in requests:
                raise ValueError("blocked_by_request must reference another request")
    for schedule in schedules:
        check_entities(schedule.entities)
        schedule_draft = drafts.get(schedule.entry_ref)
        if schedule_draft is None or schedule_draft.kind != "일정":
            raise ValueError("schedule entry_ref must reference an 일정 draft")
        if schedule.blocked_by_request != schedule_draft.blocked_by_request:
            raise ValueError("schedule must preserve its draft execution block")


class Edit(Contract):
    """RFC 6901 pointer and replacement value, not array-dot syntax.

    Empty pointer is valid RFC 6901 (whole document). VerdictHandler must restrict
    editable fields, resolve pointers and revalidate the complete Proposal.
    """

    path: Pointer
    value: JsonValue


VERDICT_CONDITIONS: dict[str, JsonValue] = {
    "oneOf": [
        {
            "properties": {
                "verdict": {"const": name},
                "edits": {"minItems": 1} if name == "edit" else {"maxItems": 0},
                "reject_reason": {"type": "string" if name == "reject" else "null"},
                "instruction": {"type": "string" if name == "revise" else "null"},
            }
        }
        for name in ("approve", "edit", "revise", "reject")
    ]
}


class Verdict(Versioned):
    """approve/edit record facts; they never resolve requests or authorize actions."""

    model_config = ConfigDict(json_schema_extra=VERDICT_CONDITIONS)
    proposal_id: ProposalId
    rev: Revision
    verdict: Literal["approve", "edit", "revise", "reject"]
    edits: list[Edit]
    reject_reason: RejectReason | None
    instruction: NonEmpty | None
    reviewed_at: Timestamp

    @model_validator(mode="after")
    def branch_inputs(self) -> Self:
        if bool(self.edits) != (self.verdict == "edit"):
            raise ValueError("only edit requires nonempty edits")
        if (self.reject_reason is not None) != (self.verdict == "reject"):
            raise ValueError("only reject requires reject_reason")
        if (self.instruction is not None) != (self.verdict == "revise"):
            raise ValueError("only revise requires instruction")
        return self


class Alert(Versioned):
    alert_id: AlertId
    proposal_id: ProposalId
    schedule_ref: Ref
    message: NonEmpty
    created_at: Timestamp


SHARED_MODELS = (
    Manifest,
    Receipt,
    Proposal,
    Verdict,
    Alert,
    Entity,
    Relation,
    Entry,
    Source,
    Thread,
)
