"""Lossless Korean Kakao text grammars; see the synthetic fixture README.

Offsets index the exact decoded input, including BOM, CRLF and message prefixes.
No text-only parser can distinguish a quoted full message line from a real one.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from weaver.schema.models import Segment, Source, Thread

_DATE = r"(?P<year>[0-9]{4})(?:년\s+|\.\s*)(?P<month>[0-9]{1,2})(?:월\s+|\.\s*)(?P<day>[0-9]{1,2})(?:일|\.)"
_CLOCK = r"(?P<period>오전|오후) (?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})"
_DAY = re.compile(r"(?:-+\s*)?" + _DATE + r"(?:\s+[월화수목금토일]요일)?(?:\s*-+)?")
_PC = re.compile(r"\[(?P<speaker>[^\]\r\n]+)\] \[" + _CLOCK + r"\] (?P<body>.*)")
_MOBILE = re.compile(_DATE + r" " + _CLOCK + r", (?P<speaker>.+?) : (?P<body>.*)")
_MOBILE_SYSTEM = re.compile(_DATE + r" " + _CLOCK + r", (?P<body>.*)")
_DATE_LIKE = re.compile(r"(?:-+\s*)?[0-9]{4}(?:년|\.)")


def _is_system(line: str) -> bool:
    """Check fixed suffixes before splitting once; avoid nested backtracking."""
    for suffix in ("님이 들어왔습니다.", "님이 나갔습니다."):
        if line.endswith(suffix):
            return len(line) > len(suffix)
    suffix = "님을 초대했습니다."
    if not line.endswith(suffix):
        return False
    inviter, separator, invited = line.removesuffix(suffix).partition("님이 ")
    return bool(inviter and separator and invited)


class KakaoParseError(ValueError):
    """Safe diagnostic code only; archived input remains available for retry."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    line: int
    start_char: int
    end_char: int


@dataclass(frozen=True)
class ParseResult:
    thread: Thread
    diagnostics: tuple[Diagnostic, ...]


def _date(match: re.Match[str]) -> date:
    return date(*(int(match[name]) for name in ("year", "month", "day")))


def _timestamp(day: date, match: re.Match[str], zone: ZoneInfo) -> datetime | None:
    hour, minute = int(match["hour"]), int(match["minute"])
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ValueError("invalid_time")
    hour = hour % 12 + (12 if match["period"] == "오후" else 0)
    naive = datetime.combine(day, time(hour, minute))
    # Ambiguous/nonexistent wall times in a configured DST zone are unknown,
    # rather than choosing one offset without evidence from the export.
    first, second = (naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1))
    if first.utcoffset() != second.utcoffset():
        return None
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return first


def parse_kakao(source: Source, text: str, *, timezone: ZoneInfo) -> ParseResult:
    """Parse supported Korean text into a v1 Thread plus content-free diagnostics.

    timezone must come from ServerConfig.time.zone; capture time is never used
    to fill missing message dates. Header-only input is preserved with a warning;
    input without any recognized export structure raises unsupported_format.
    """
    if source.kind != "text" or source.channel != "kakao":
        raise ValueError("expected text/kakao Source")
    if not text.strip("\ufeff \t\r\n"):
        raise KakaoParseError("empty_input")
    if "\x00" in text:
        raise KakaoParseError("unsupported_encoding")
    segments: list[Segment] = []
    diagnostics: list[Diagnostic] = []
    day: date | None = None
    active: tuple[int, str, datetime | None] | None = None
    recognized = False
    message_count = 0
    continuation = False
    previous_time: datetime | None = None

    def diagnostic(code: str, line: int, start: int, end: int) -> None:
        diagnostics.append(Diagnostic(code, line, start, end))

    def append(
        start: int,
        end: int,
        speaker: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        if end > start:
            segments.append(
                Segment(
                    segment_id=f"s{len(segments) + 1}",
                    start_char=start,
                    end_char=end,
                    start_ms=None,
                    end_ms=None,
                    speaker=speaker,
                    occurred_at=occurred_at,
                )
            )

    def flush(end: int) -> None:
        nonlocal active, continuation
        if active is not None:
            append(active[0], end, active[1], active[2])
            active = None
        continuation = False

    start = 0
    # Only LF/CRLF are line delimiters. Unicode separators in messages are data.
    lines = text.split("\n")
    for number, part in enumerate(lines, 1):
        raw = part + ("\n" if number < len(lines) else "")
        if not raw:
            continue
        end = start + len(raw)
        line = raw.removesuffix("\n").removesuffix("\r")
        if start == 0:
            line = line.removeprefix("\ufeff")
        date_match = _DAY.fullmatch(line)
        message = _PC.fullmatch(line) or _MOBILE.fullmatch(line)
        mobile_system = _MOBILE_SYSTEM.fullmatch(line)
        is_system = _is_system(line) or (
            mobile_system is not None and _is_system(mobile_system["body"])
        )
        is_header = line.endswith("님과 카카오톡 대화") or line.startswith(
            "저장한 날짜 : "
        )
        if date_match:
            recognized = True
            flush(start)
            try:
                day = _date(date_match)
            except ValueError:
                day = None
                diagnostic("invalid_date", number, start, end)
            append(start, end)
        elif message:
            recognized = True
            ambiguous = active is not None and continuation
            flush(start)
            occurred_at = None
            invalid = False
            message_day = day
            if "year" in message.groupdict():
                try:
                    message_day = _date(message)
                except ValueError:
                    message_day = None
                    invalid = True
                    diagnostic("invalid_date", number, start, end)
            try:
                # Validate the clock even when the date is unknown.
                value = _timestamp(message_day or date(2000, 1, 1), message, timezone)
                if message_day is None:
                    diagnostic("missing_date", number, start, end)
                else:
                    occurred_at = value
                    if value is None:
                        diagnostic("ambiguous_local_time", number, start, end)
            except ValueError:
                invalid = True
                diagnostic("invalid_time", number, start, end)
            if invalid:
                append(start, end)
            else:
                if ambiguous:
                    diagnostic("ambiguous_boundary", number, start, end)
                header_mismatch = (
                    "year" in message.groupdict()
                    and day is not None
                    and message_day != day
                )
                if header_mismatch or (
                    occurred_at is not None
                    and previous_time is not None
                    and occurred_at < previous_time
                ):
                    diagnostic("time_regression", number, start, end)
                if occurred_at is not None:
                    previous_time = occurred_at
                message_count += 1
                active = start, message["speaker"], occurred_at
        elif is_system or is_header:
            recognized = True
            flush(start)
            append(start, end)
        elif active is None and _DATE_LIKE.match(line):
            flush(start)
            day = None  # Never inherit yesterday through a broken day boundary.
            diagnostic("unrecognized_line", number, start, end)
            append(start, end)
        elif active is None:
            append(start, end)
            if line.strip():
                diagnostic("unrecognized_line", number, start, end)
        else:
            # Blank/partial date or message lookalikes inside a message are body.
            continuation = True
        start = end
    flush(len(text))
    if not recognized:
        raise KakaoParseError("unsupported_format")
    if not message_count:
        diagnostic("no_messages", 1, 0, len(text))
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
