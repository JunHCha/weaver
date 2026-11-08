# Synthetic Kakao exports

All people, room titles and messages here are invented. These are grammar
fixtures, not exports copied from a personal conversation or a real device.

Supported baseline: Korean PC bracketed messages and Korean mobile dated CSV-like
messages, UTF-8 with optional leading BOM, LF or CRLF. Exact Kakao app versions
and device compatibility are **unverified**; no claim of all-version support.
Approved by the coordinator/user on 2026-11-07 (msg_d4c0f11d1fe4).

| Variant | Date boundary | Message |
| --- | --- | --- |
| PC | `--------------- 2026년 11월 7일 토요일 ---------------` (dashes/weekday optional) | `[홍길동] [오전 9:00] 본문` |
| Mobile Korean | `2026년 11월 7일 토요일` | `2026년 11월 7일 오전 9:00, 홍길동 : 본문` |
| Mobile dotted | `2026. 11. 7.` | `2026. 11. 7. 오후 1:00, 홍길동 : 본문` |

Hours use 오전/오후 1–12 (12 AM = midnight, 12 PM = noon). Commas and colons
inside message bodies remain literal. Mobile speakers end at the first ` : `;
PC speakers cannot contain `]`. Other name/delimiter variants are unverified.
Supported system lines end in `님이 들어왔습니다.`, `님이 나갔습니다.` or
`님이 …님을 초대했습니다.`; mobile system lines may have a dated prefix.
Other system-like lines inside a message remain continuation text; without
an active message they are raw segments with `unrecognized_line` diagnostics.
Date/header/system patterns appearing literally inside a body also form boundaries.

`Thread.text` is the exact UTF-8 decoded payload, including BOM, line endings,
headers and prefixes. Ordered `s1`, `s2`, … segments partition every code point
without gaps. A message segment starts at its entire prefix line and extends
through continuation lines up to the next recognized boundary. Header/date/system
and malformed boundary segments have null speaker/time; audio ms are always null.
Line numbers in diagnostics are 1-based; ranges are half-open code point offsets.

An exact message-shaped quoted line cannot be distinguished from a real message.
The grammar splits it and emits `ambiguous_boundary` when a message-shaped line
follows continuation lines in the active message. Ordinary consecutive single-line
messages do not warn; this heuristic cannot detect every quoted boundary.
Partial lookalikes stay in the body;
invalid clocks/dates are raw segments with diagnostics. A broken date boundary
clears the previous date only outside an active message. Inside a message, partial
date lookalikes remain body and preserve the day; an exact date-header grammar match
is still a boundary. Time reversal or a mobile date differing from the latest header
emits `time_regression` without adjusting the timestamp. Source occurred_at remains distinct from message times
and captured_at is never used as a fallback. `[time].timezone` from ServerConfig
(default Asia/Seoul) is explicitly injected; DST folds/gaps yield null and a
safe diagnostic rather than an invented instant.

Empty/whitespace-only input raises `empty_input` (v1 Thread.text is nonempty).
No recognized structure raises `unsupported_format`; header-only input is retained
with `no_messages`. English and other unsupported formats are rejected when no
supported structure is present; mixed unknown lines are retained and diagnosed.
CP949/UTF-16 are rejected by TextSpinner with `unsupported_encoding` rather than
lossy decoding. The archive remains unchanged on every error.

Integration: `parse_kakao(source, text, timezone=...) -> ParseResult` exposes full
diagnostics; `weaver.spinner.text.text_to_thread` returns Thread and logs only
one count summary per diagnostic code. `TextSpinner.from_config(store, settings).spin(source)`
reads through ArchiveStore.locate and verifies the original byte SHA-256 against
Source.sha256, raising `payload_mismatch` on corruption before decoding. The general Spinner (#4) can delegate its text
branch to this adapter. `share_text` bypasses Kakao parsing, retaining the original
as one segment. No schema v1 changes, LLM, STT or CLI were needed.

## Galaxy call scripts (#56)

`call.txt` is invented, including its title, names and utterances. It follows the
structural example on issue #56; it contains no real call content. Server input
is UTF-8 (Android #65 converts the UTF-16 BOM original without losing content).
Optional Unicode BOM, LF/CRLF and trailing whitespace are retained exactly.

`weaver.loom.call.parse_call(source, text)` returns `ParseResult`. Every
nonempty line's content, excluding its LF/CRLF, is exactly one Segment. The
`AI로 생성된 콘텐츠입니다` line, the following nonblank title, whitespace-only
lines and each header `<speaker>  (MM:SS)` are un-timed, un-speakered structural
Segments. Thread has no separate title field; adding one would unnecessarily
change v1. The single nonblank body line after a header is the speech Segment
and alone carries speaker, start_ms, end_ms and occurred_at. A body line is any
line that is not a header, even if it contains `  (`.
`H:MM:SS` accepts 1–9 hour digits; minutes/seconds are exactly two ASCII digits
in 00–59. Exact header lookalikes in speech split blocks: text cannot distinguish
quotation from a real header. A missing blank separator is diagnosed.

Line endings and empty lines lie between Segments: the schema forbids empty
Segments, and absorbing terminators into some Segments but not speech bodies
would make ranges inconsistent. The gaps are whitespace only and the Reviser
restores them verbatim, so Thread.text and every range stay exact. Speakers
are unchanged, including `나 & 상대` (`overlap_speakers`). start_ms is elapsed
call time; end_ms is the next recognized block's start only when strictly later.
Equal/reversed starts diagnose `non_increasing_time` and leave that end null;
the last end is always null. occurred_at is Source.occurred_at + elapsed time,
or null if call time is absent/unrepresentable. captured_at is never a fallback.

Empty/header-only blocks retain their header with `empty_utterance` and have no
speech Segment; their start still ends the previous body. Unknown and
malformed lines remain un-timed preservation Segments with `unrecognized_line`
or `invalid_header`; extra body lines are not silently accepted as normal speech.
Missing preamble/title and no blocks are diagnosed. Entirely unrecognized,
empty or NUL-containing inputs fail with safe codes. Diagnostic fields are code,
1-based line and code-point range only. GalaxyCallParser implements Spinner's
`TextParser.parse(Source, str) -> Thread`, logs code/count only and advertises
`galaxy.call.v1`; Spinner installs it by default and still requires a Reviser.
No regex is used; fixed string splits and bounded integer conversion avoid
backtracking and huge-int parsing. Tests include megabyte adversarial lines.
