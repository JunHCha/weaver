"""Spinner text path over the archive's public locator boundary."""

import hashlib
import logging
from collections import Counter
from typing import Protocol
from zoneinfo import ZoneInfo

from weaver.config import ServerConfig
from weaver.loom.kakao import KakaoParseError, parse_kakao
from weaver.schema.internal import ArchivedSource
from weaver.schema.models import Script, Segment, Source

PARSER_VERSION = "spinner.text.v1"

_LOG = logging.getLogger(__name__)


class SourceLocator(Protocol):
    def locate(self, source_id: str) -> ArchivedSource: ...


def text_to_thread(source: Source, text: str, *, timezone: ZoneInfo) -> Script:
    """No LLM or STT. Share text bypasses all Kakao recognition."""
    if source.kind != "text":
        raise ValueError("expected text Source")
    if not text.strip("\ufeff \t\r\n"):
        raise KakaoParseError("empty_input")
    if source.channel not in {"kakao", "share_text"}:
        raise ValueError("unsupported_text_channel")
    if source.channel == "kakao":
        result = parse_kakao(source, text, timezone=timezone)
        # Never log text, speaker, archive path or timestamps.
        for code, count in Counter(d.code for d in result.diagnostics).items():
            _LOG.warning("kakao:%s count=%d", code, count)
        return result.thread
    return Script(
        schema_version=1,
        source_id=source.source_id,
        channel=source.channel,
        occurred_at=source.occurred_at,
        text=text,
        segments=[
            Segment(
                segment_id="s1",
                start_char=0,
                end_char=len(text),
                start_ms=None,
                end_ms=None,
                speaker=None,
                occurred_at=None,
            )
        ],
    )


class TextSpinner:
    """Implements spin(Source)->Script for text without changing Spinner's port."""

    def __init__(self, store: SourceLocator, *, timezone: ZoneInfo) -> None:
        self._store = store
        self._timezone = timezone

    @classmethod
    def from_config(cls, store: SourceLocator, config: ServerConfig) -> "TextSpinner":
        return cls(store, timezone=config.time.zone)

    def spin(self, source: Source) -> Script:
        if source.kind != "text":
            raise ValueError("expected text Source")
        archived = self._store.locate(source.source_id)
        if archived.source != source:
            raise ValueError("archive Source mismatch")
        payload = archived.payload_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != source.sha256:
            raise KakaoParseError("payload_mismatch")
        try:
            text = payload.decode("utf-8")
        except UnicodeError:
            raise KakaoParseError("unsupported_encoding") from None
        if "\x00" in text:
            raise KakaoParseError("unsupported_encoding")
        return text_to_thread(source, text, timezone=self._timezone)
