"""Offline, versioned Weaver bundle contract (not a Webex REST response).

Raw identifiers and attachment URLs stay in the archive/private index. The Script
contains only message text, display speakers and timestamps. No network or LLM.
"""

import os
import sqlite3
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

    @model_validator(mode="after")
    def unique_messages(self) -> Self:
        ids = [message.message_id for message in self.messages]
        if len(set(ids)) != len(ids):
            raise ValueError("webex_duplicate_message")
        return self


class WebexProvenance(Contract):
    source_id: str
    sha256: str
    instance_id: NonEmpty
    space_id: NonEmpty
    # Position corresponds to Script segment s1, s2, ...; IDs never enter Script.
    messages: list[WebexMessage]


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
            messages=messages,
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
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute(
                "CREATE TABLE IF NOT EXISTS provenance "
                "(source_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )

    def save(self, value: WebexProvenance) -> None:
        body = value.model_dump_json()
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute(
                "INSERT OR IGNORE INTO provenance(source_id,body) VALUES(?,?)",
                (value.source_id, body),
            )
            row = db.execute(
                "SELECT body FROM provenance WHERE source_id=?", (value.source_id,)
            ).fetchone()
            if row is None or row[0] != body:
                raise ValueError("webex_provenance_conflict")

    def get(self, source_id: str) -> WebexProvenance | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT body FROM provenance WHERE source_id=?", (source_id,)
            ).fetchone()
        return WebexProvenance.model_validate_json(row[0]) if row else None

    def resolve(self, source_id: str) -> tuple[str, str] | None:
        value = self.get(source_id)
        return ("webex", value.space_id) if value is not None else None


class WebexParser:
    version = "spinner.webex.v1"

    def __init__(self, provenance: WebexProvenanceStore) -> None:
        self.provenance = provenance

    def parse(self, source: Source, text: str) -> Script:
        script, provenance = parse_webex(source, text)
        # Commit before Spinner checkpoints success; crash replay is idempotent.
        self.provenance.save(provenance)
        return script
