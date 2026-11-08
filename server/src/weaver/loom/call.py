"""Lossless Galaxy call scripts decoded as UTF-8 by Android (ADR-028).

Only LF/CRLF delimit lines; offsets are Unicode code points. Recognition uses
fixed splits and bounded clock fields, with no regex backtracking or huge ints.
"""

import logging
from collections import Counter
from datetime import timedelta

from weaver.loom.kakao import Diagnostic, ParseResult
from weaver.schema import Source, Thread
from weaver.schema.models import Segment

_NOTICE = "AI로 생성된 콘텐츠입니다"
_LOG = logging.getLogger(__name__)


class CallParseError(ValueError):
    """Content-free failure code; never include input or speaker names."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _header(line: str) -> tuple[str, int] | None:
    speaker, separator, clock = line.rpartition("  (")
    if not separator or not speaker.strip() or not clock.endswith(")"):
        return None
    fields = clock[:-1].split(":")
    if len(fields) not in (2, 3):
        return None
    if any(not f.isascii() or not f.isdecimal() for f in fields):
        return None
    if any(len(f) != 2 for f in fields[-2:]):
        return None
    if len(fields) == 3 and not 1 <= len(fields[0]) <= 9:
        return None
    values = [int(f) for f in fields]
    if any(v > 59 for v in values[-2:]):
        return None
    seconds = values[-2] * 60 + values[-1]
    if len(values) == 3:
        seconds += values[0] * 3600
    return speaker, seconds * 1000


def parse_call(source: Source, text: str) -> ParseResult:
    """Retain preamble, headers, bodies, blank and unknown lines without edits.

    A block contains one body line; surplus lines are preserved with diagnostics.
    Header lookalikes in body position are inherently ambiguous and split blocks.
    Missing/invalid structure is diagnostic; entirely unrecognized input fails.
    """
    if source.kind != "text" or source.channel != "call":
        raise CallParseError("expected_text_call")
    if not text.strip("\ufeff \t\r\n"):
        raise CallParseError("empty_input")
    if "\x00" in text:
        raise CallParseError("unsupported_encoding")
    segments: list[Segment] = []
    diagnostics: list[Diagnostic] = []
    blocks: list[tuple[int, int, int, int]] = []
    active: tuple[int, int, str, int, int] | None = None
    has_body = False
    recognized = False
    title_pending = False

    def warn(code: str, number: int, start: int, end: int) -> None:
        diagnostics.append(Diagnostic(code, number, start, end))

    def append(start: int, end: int) -> None:
        if end > start:
            segments.append(
                Segment(
                    segment_id=f"s{len(segments) + 1}",
                    start_char=start,
                    end_char=end,
                    start_ms=None,
                    end_ms=None,
                    speaker=None,
                    occurred_at=None,
                )
            )

    def flush(end: int) -> None:
        nonlocal active
        if active is None:
            return
        start, number, speaker, ms, header_end = active
        append(start, end)
        segment = segments[-1]
        segment.speaker = speaker
        segment.start_ms = ms
        if source.occurred_at is not None:
            try:
                segment.occurred_at = source.occurred_at + timedelta(milliseconds=ms)
            except OverflowError:
                warn("occurred_at_overflow", number, start, header_end)
        if not has_body:
            warn("empty_utterance", number, start, header_end)
        blocks.append((len(segments) - 1, number, start, header_end))
        active = None

    start = 0
    parts = text.split("\n")
    for number, part in enumerate(parts, 1):
        raw = part + ("\n" if number < len(parts) else "")
        if not raw:
            continue
        end = start + len(raw)
        line = part.removesuffix("\r")
        if start == 0:
            line = line.removeprefix("\ufeff")
        if number == 1 and line != _NOTICE:
            warn("missing_preamble", number, start, end)
        header = _header(line)
        if header is not None:
            recognized = True
            if title_pending:
                warn("missing_title", number, start, end)
                title_pending = False
            if active is not None and has_body:
                warn("missing_separator", number, start, end)
            flush(start)
            speaker, ms = header
            active = start, number, speaker, ms, end
            has_body = False
            if " & " in speaker:
                warn("overlap_speakers", number, start, end)
        elif active is not None and not has_body and line.strip() and "  (" not in line:
            has_body = True
        else:
            flush(start)
            append(start, end)
            if number == 1 and line == _NOTICE:
                recognized = True
                title_pending = True
            elif title_pending and line.strip():
                title_pending = False
            elif line.strip():
                warn(
                    "invalid_header" if "  (" in line else "unrecognized_line",
                    number,
                    start,
                    end,
                )
        start = end
    flush(len(text))
    if not recognized:
        raise CallParseError("unsupported_format")
    if title_pending:
        warn("missing_title", 1, 0, len(text))
    if not blocks:
        warn("no_messages", 1, 0, len(text))
    for previous, current in zip(blocks, blocks[1:], strict=False):
        segment = segments[previous[0]]
        next_ms = segments[current[0]].start_ms
        assert segment.start_ms is not None and next_ms is not None
        if next_ms > segment.start_ms:
            segment.end_ms = next_ms
        else:
            warn("non_increasing_time", current[1], current[2], current[3])
    return ParseResult(
        Thread(
            schema_version=1,
            source_id=source.source_id,
            channel=source.channel,
            occurred_at=source.occurred_at,
            text=text,
            segments=segments,
        ),
        tuple(diagnostics),
    )


class GalaxyCallParser:
    """Spinner TextParser adapter with content-free diagnostic summaries."""

    version = "galaxy.call.v1"

    def parse(self, source: Source, text: str) -> Thread:
        result = parse_call(source, text)
        for code, count in Counter(d.code for d in result.diagnostics).items():
            _LOG.warning("call:%s count=%d", code, count)
        return result.thread
