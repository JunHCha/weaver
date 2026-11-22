"""Offline, versioned Weaver bundle contract (not a Webex REST response).

Raw identifiers and attachment URLs stay in the archive/private index. The Script
contains only message text, display speakers and timestamps. No network or LLM.
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from weaver.schema import Script, Segment, Source
from weaver.schema.models import Contract, NonEmpty, Timestamp


class WebexAttachment(Contract):
    name: NonEmpty | None = None
    media_type: NonEmpty | None = None
    url: NonEmpty | None = None


class WebexMessage(Contract):
    message_id: NonEmpty
    parent_id: NonEmpty | None = None
    author_display: NonEmpty | None
    created_at: Timestamp
    updated_at: Timestamp | None = None
    text: str
    attachments: list[WebexAttachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_content(self) -> Self:
        if not self.text.strip() and not self.attachments:
            raise ValueError("webex_empty_message")
        return self


class WebexBundle(Contract):
    format: Literal["weaver.webex.bundle.v1"]
    instance_id: NonEmpty
    space_id: NonEmpty
    messages: list[WebexMessage] = Field(min_length=1)
    # Frozen-bundle identity. Collector buffers leave the defaults; closing a
    # Space-day writes them so the archived bytes alone name the day, the part
    # and, for a correction, the capture it corrects (never Script content).
    day: NonEmpty | None = None
    part: int = Field(default=0, ge=0)
    part_count: int = Field(default=1, ge=1)
    revision: int = Field(default=0, ge=0)
    corrects_capture_id: NonEmpty | None = None

    @model_validator(mode="after")
    def unique_messages(self) -> Self:
        ids = [message.message_id for message in self.messages]
        if len(set(ids)) != len(ids):
            raise ValueError("webex_duplicate_message")
        return self


class WebexMessageRef(Contract):
    message_id: NonEmpty
    parent_id: NonEmpty | None = None
    updated_at: Timestamp | None = None
    segment_id: NonEmpty


class WebexProvenance(Contract):
    format_version: Literal[1] = 1
    source_id: str
    sha256: str
    instance_id: NonEmpty
    space_id: NonEmpty
    # Position corresponds to Script segment s1, s2, ...; IDs never enter Script.
    messages: list[WebexMessageRef]
    # Space-day identity of a closed bundle; revision > 0 is a correction Source
    # that links to the capture it corrects and never rewrites an approved Thread.
    day: NonEmpty | None = None
    part: int = 0
    part_count: int = 1
    revision: int = 0
    corrects_capture_id: NonEmpty | None = None


def parse_webex(source: Source, text: str) -> tuple[Script, WebexProvenance]:
    """Parse an internal payload, sorting by UTC creation time then message ID."""
    if source.channel != "webex" or source.kind != "text":
        raise ValueError("webex_expected_text_source")
    try:
        bundle = WebexBundle.model_validate_json(text)
    except ValidationError:
        # Do not expose raw payload/identifiers through Pydantic diagnostics.
        raise ValueError("webex_invalid_bundle") from None
    messages = sorted(
        bundle.messages,
        key=lambda message: (message.created_at.astimezone(UTC), message.message_id),
    )
    parts: list[str] = []
    segments: list[Segment] = []
    offset = 0
    for number, message in enumerate(messages, 1):
        body = message.text
        if message.attachments:
            # Names/URLs may themselves carry private identifiers: expose existence.
            marker = f"[첨부 {len(message.attachments)}개 · 다운로드하지 않음]"
            body = f"{body}\n{marker}" if body.strip() else marker
        speaker = " ".join((message.author_display or "작성자 미상").splitlines())
        prefix = f"[{message.created_at.isoformat()} · {speaker}] "
        body = "\n".join(
            prefix + line for line in body.replace("\r\n", "\n").split("\n")
        )
        parts.append(body)
        segments.append(
            Segment(
                segment_id=f"s{number}",
                start_char=offset,
                end_char=offset + len(body),
                start_ms=None,
                end_ms=None,
                speaker=message.author_display,
                occurred_at=message.created_at,
            )
        )
        offset += len(body) + 1
    return (
        Script(
            schema_version=1,
            source_id=source.source_id,
            channel="webex",
            occurred_at=source.occurred_at,
            text="\n".join(parts),
            segments=segments,
        ),
        WebexProvenance(
            source_id=source.source_id,
            sha256=source.sha256,
            instance_id=bundle.instance_id,
            space_id=bundle.space_id,
            day=bundle.day,
            part=bundle.part,
            part_count=bundle.part_count,
            revision=bundle.revision,
            corrects_capture_id=bundle.corrects_capture_id,
            messages=[
                WebexMessageRef(
                    message_id=message.message_id,
                    parent_id=message.parent_id,
                    updated_at=message.updated_at,
                    segment_id=f"s{number}",
                )
                for number, message in enumerate(messages, 1)
            ],
        ),
    )


class WebexProvenanceStore:
    """Private archive-source lookup; structurally matches B2's resolver port."""

    def __init__(self, state_root: Path) -> None:
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = state_root / "webex-provenance.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute(
                "CREATE TABLE IF NOT EXISTS provenance "
                "(source_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            db.execute("PRAGMA secure_delete=ON")
            for source_id, body in db.execute(
                "SELECT source_id,body FROM provenance"
            ).fetchall():
                value = self._decode(body)
                db.execute(
                    "UPDATE provenance SET body=? WHERE source_id=?",
                    (value.model_dump_json(), source_id),
                )

    @staticmethod
    def _decode(body: str) -> WebexProvenance:
        raw = json.loads(body)
        if "format_version" not in raw:
            # Unreleased initial format duplicated archive content. Strip it on
            # upgrade, keeping only the lookup identity and segment mapping.
            raw["format_version"] = 1
            raw["messages"] = [
                {
                    "message_id": message["message_id"],
                    "parent_id": message.get("parent_id"),
                    "updated_at": message.get("updated_at"),
                    "segment_id": f"s{number}",
                }
                for number, message in enumerate(raw["messages"], 1)
            ]
        return WebexProvenance.model_validate(raw)

    def save(self, value: WebexProvenance) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM provenance WHERE source_id=?", (value.source_id,)
            ).fetchone()
            if row is not None:
                previous = self._decode(row[0])
                if (previous.sha256, previous.instance_id, previous.space_id) != (
                    value.sha256,
                    value.instance_id,
                    value.space_id,
                ):
                    raise ValueError("webex_provenance_conflict")
            # Refresh derived mapping/defaults, never confuse model evolution
            # with changes to the immutable archived identity.
            db.execute(
                "INSERT INTO provenance(source_id,body) VALUES(?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET body=excluded.body",
                (value.source_id, value.model_dump_json()),
            )

    def get(self, source_id: str) -> WebexProvenance | None:
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute(
                "SELECT body FROM provenance WHERE source_id=?", (source_id,)
            ).fetchone()
        return self._decode(row[0]) if row else None

    def siblings(self, space_id: str, day: str) -> list[WebexProvenance]:
        """Every known Source of one Space-day (all parts and revisions)."""
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = db.execute("SELECT body FROM provenance").fetchall()
        found = [self._decode(body) for (body,) in rows]
        return [v for v in found if v.space_id == space_id and v.day == day]

    def resolve(self, source_id: str) -> tuple[str, str] | None:
        value = self.get(source_id)
        return ("webex", value.space_id) if value is not None else None


class WebexParser:
    version = "spinner.webex.v2"

    def __init__(self, provenance: WebexProvenanceStore) -> None:
        self.provenance = provenance

    def parse(self, source: Source, text: str) -> Script:
        script, provenance = parse_webex(source, text)
        # Commit before Spinner checkpoints success; crash replay is idempotent.
        self.provenance.save(provenance)
        return script
