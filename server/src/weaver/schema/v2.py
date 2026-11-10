"""Schema v2 data contracts for the review and append-only log boundary."""

from typing import Annotated, Literal, Protocol, Self, cast

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from .models import Contract, Digest, NonEmpty, Revision, SourceId, Timestamp


class TagCorrection(Contract):
    """Changes to the effective tags of an existing thread."""

    target: NonEmpty
    remove: list[NonEmpty]
    add: list[NonEmpty]


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

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
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


type Stage = Literal["수집", "전송", "정리", "질문", "검토", "반영", "wiki갱신"]


class ReviewPackage(Version2):
    """One source's draft and bounded questions for phone review."""

    package_id: NonEmpty
    source_id: SourceId
    revision: Revision
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
                    "if": {"properties": {"action": {"const": action}}},
                    "then": {"properties": {field: {"type": "string"}}},
                    "else": {"properties": {field: {"type": "null"}}},
                }
                for action, field in [("edit", "edited_body"), ("feedback", "feedback")]
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
        if (self.edited_body is not None) != (self.action == "edit"):
            raise ValueError("edited_body is required exactly for edit")
        if (self.feedback is not None) != (self.action == "feedback"):
            raise ValueError("feedback is required exactly for feedback")
        return self
