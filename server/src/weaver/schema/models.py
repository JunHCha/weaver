"""Wire contracts shared by the phone and the server.

Capture files (Manifest, Receipt, Source, Script) carry ``schema_version`` 1.
Review, Fabric and Alert files carry ``schema_version`` 2; consumers reject any
other version. All keys are required, including nullable keys. Unknown fields
fail closed. IDs use canonical uppercase Crockford ULIDs (first digit at most 7).
"""

import re
from datetime import datetime
from typing import Annotated, Literal, Protocol, Self, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
type AlertId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=rf"^alt_{ULID}$", min_length=30, max_length=30
    ),
]
type Ref = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z][1-9][0-9]*$"),
    Field(json_schema_extra={"not": {"pattern": r"[\r\n]"}}),
]
type NonEmpty = Annotated[str, StringConstraints(strict=True, pattern=r"\S")]
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

    Segment IDs are unique within a Script. Audio offsets are integer milliseconds
    from payload start. A known start permits an unknown (null) end; a known
    end requires a strictly earlier start. occurred_at and speaker preserve
    message/call provenance.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"start_ms": {"type": "null"}}},
            "then": {"properties": {"end_ms": {"type": "null"}}},
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
        if self.start_ms is None and self.end_ms is not None:
            raise ValueError("end_ms requires start_ms")
        if self.start_ms is not None and self.end_ms is not None:
            if self.end_ms <= self.start_ms:
                raise ValueError("audio range must be nonempty and ordered")
        return self


class Script(Versioned):
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
                raise ValueError("segment exceeds Script text")
        return self


class TagCorrection(Contract):
    """Changes to the effective tags of an existing thread."""

    target: NonEmpty
    remove: list[NonEmpty]
    add: list[NonEmpty]


class ScheduleValue(Contract):
    """Structured times of a schedule, with an explicit IANA timezone."""

    start: Timestamp
    end: Timestamp | None
    timezone: NonEmpty

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must follow start")
        return self


class ThreadDraft(Contract):
    """An approved or human-authored fact before a sink assigns its sequence."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": cast(
                JsonValue,
                [
                    {
                        "if": {"properties": {"kind": {"const": kind}}},
                        "then": {"properties": {field: {"type": yes}}},
                        "else": {"properties": {field: {"type": no}}},
                    }
                    for kind, field, yes, no in [
                        ("요청", "direction", "string", "null"),
                        ("태그정정", "correction", "object", "null"),
                        ("거절", "content", "null", "string"),
                    ]
                ]
                + [
                    {
                        "if": {"properties": {"kind": {"not": {"const": "결정"}}}},
                        "then": {"properties": {"resolves": {"type": "null"}}},
                    }
                ],
            )
        }
    )

    thread_id: NonEmpty
    kind: Literal["요청", "결정", "일정", "기록", "태그정정", "거절"]
    content: NonEmpty | None
    occurred_at: Timestamp
    recorded_at: Timestamp
    tags: list[NonEmpty]
    field: Literal["deadline", "schedule", "assignees", "status"] | None
    value: JsonValue
    source_id: SourceId
    actor: Literal["ai_approved", "human"]
    direction: Literal["받음", "보냄"] | None
    resolves: NonEmpty | None
    retracts: NonEmpty | None
    correction: TagCorrection | None
    blocked_by: NonEmpty | None = None

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        if self.blocked_by is not None and self.kind != "일정":
            raise ValueError("only schedules can be blocked")
        if self.field == "schedule" and self.value is not None:
            ScheduleValue.model_validate(self.value)
        if (self.direction is not None) != (self.kind == "요청"):
            raise ValueError("direction is required exactly for requests")
        if (self.correction is not None) != (self.kind == "태그정정"):
            raise ValueError("correction is required exactly for tag corrections")
        if self.resolves is not None and self.kind != "결정":
            raise ValueError("only decisions can resolve requests")
        if (self.content is None) != (self.kind == "거절"):
            raise ValueError("content is absent exactly for rejections")
        return self


class Thread(ThreadDraft):
    """A stored thread with the sequence assigned by its sink."""

    seq: Annotated[int, Field(strict=True, ge=1)]


class ThreadSink(Protocol):
    """Append drafts and return stored threads with assigned sequences.

    Replaying an identical batch of thread IDs must return the existing rows
    without appending duplicates, even if an earlier call lost its response.
    Conflicting content for an existing ID must be rejected atomically.
    """

    def append(self, threads: list[ThreadDraft]) -> list[Thread]: ...


class Question(Contract):
    """A low-confidence question and exactly three choices."""

    source_excerpt: str = ""
    question: NonEmpty
    rationale: NonEmpty
    choices: Annotated[list[NonEmpty], Field(min_length=3, max_length=3)]


class Version2(Contract):
    """Version discriminator for v2 wire files."""

    schema_version: Literal[2]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 2")
        return value


class EventSummary(Version2):
    """Content-addressed approved summary and its predecessor, if any."""

    summary_id: Digest
    source_id: SourceId
    script: NonEmpty
    body: NonEmpty
    replaces: Digest | None
    created_at: Timestamp


class ApprovedSchedule(Version2, ScheduleValue):
    """A published schedule from an approved summary and a Fabric thread."""

    revision: Revision = 1
    retracted: bool = False
    package_id: NonEmpty
    thread_id: NonEmpty
    title: NonEmpty
    blocked_by: NonEmpty | None
    summary_id: Digest


type Stage = Literal["수집", "전송", "정리", "질문", "검토", "반영", "wiki갱신"]


class ReviewPackage(Version2):
    """One source's draft and bounded questions for phone review."""

    package_id: NonEmpty
    source_id: SourceId
    revision: Revision
    title: str = ""
    source_text: str = ""
    source_type: str = ""
    draft_body: NonEmpty
    questions: Annotated[list[Question], Field(max_length=3)]
    stage: Stage
    stage_times: dict[Stage, Timestamp]


class Verdict(Version2):
    """A user's action on one exact review package revision."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "feedback"}}},
                    "then": {"properties": {"feedback": {"type": "string"}}},
                },
                {
                    "if": {"properties": {"action": {"const": "reject"}}},
                    "then": {"properties": {"feedback": {"type": "null"}}},
                },
                {
                    "if": {"properties": {"action": {"const": "edit"}}},
                    "then": {"properties": {"edited_body": {"type": "string"}}},
                },
                {
                    "if": {"properties": {"action": {"enum": ["approve", "reject"]}}},
                    "then": {"properties": {"edited_body": {"type": "null"}}},
                },
            ]
        }
    )

    package_id: NonEmpty
    revision: Revision
    action: Literal["approve", "edit", "feedback", "reject"]
    answers: list[NonEmpty]
    edited_body: NonEmpty | None
    feedback: NonEmpty | None

    @model_validator(mode="after")
    def action_inputs(self) -> Self:
        if self.action == "edit" and self.edited_body is None:
            raise ValueError("edited_body is required for edit")
        if self.action in {"approve", "reject"} and self.edited_body is not None:
            raise ValueError("edited_body is accepted only for edit or feedback")
        if self.action == "feedback" and self.feedback is None:
            raise ValueError("feedback is required for feedback")
        if self.action == "reject" and self.feedback is not None:
            raise ValueError("feedback is not accepted for reject")
        return self


class Alert(Version2):
    """Pre-approval notice that a new schedule may change an approved one."""

    alert_id: AlertId
    package_id: NonEmpty
    thread_id: NonEmpty
    message: NonEmpty
    created_at: Timestamp


SHARED_MODELS = (
    Manifest,
    Receipt,
    Source,
    Script,
    ApprovedSchedule,
    Thread,
    ThreadDraft,
    EventSummary,
    ReviewPackage,
    Question,
    Verdict,
    Alert,
)
