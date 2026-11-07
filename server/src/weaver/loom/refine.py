"""Deterministic Korean rule selection; never interpret dates or call an LLM.

Coordinates are half-open Unicode offsets into Thread.text. Window units are
sentences, bounded additionally by every Segment edge and newline. Overlapping
windows merge; merely adjacent windows stay separate. See RuleRefiner.select.
"""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from weaver.config import RefinerConfig
from weaver.schema import Thread
from weaver.schema.internal import Candidate, CandidateTrigger
from weaver.schema.models import Segment

_WORD = r"가-힣A-Za-z0-9_"
_LEFT = rf"(?<![{_WORD}.:])"
# Korean particles/endings are open-ended; keep the signal's exact range and
# allow following Hangul. Known lexical false positives are excluded below.
_RIGHT = r"(?![A-Za-z0-9_:])"
_MONTH = r"(?:1[0-2]|0?[1-9])"
_DAY = r"(?:3[01]|[12][0-9]|0?[1-9])"
_HOUR = r"(?:2[0-3]|1[0-9]|0?[0-9])"
_MINUTE = r"(?:[0-5]?[0-9])"
_NUMBER = r"(?:[1-9][0-9]{0,2}|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)"
_DATE = (
    rf"(?:[0-9]{{4}}\s*년\s*)?{_MONTH}\s*월(?:\s*{_DAY}\s*일)?"
    r"(?!\s*[0-9]+\s*일)"
    rf"|[0-9]{{4}}-{_MONTH}-{_DAY}"
    rf"|[0-9]{{4}}\.\s*{_MONTH}\.\s*{_DAY}\.?(?![0-9])"
    r"|[0-9]{4}\s*년(?!\s*[0-9]+\s*월)"
    rf"|{_DAY}\s*일"
    r"|오늘|어제|내일|모레|그제|(?:이번|다음|지난)\s*(?:주|달|해|월|년)"
    r"|[월화수목금토일]요일"
)
_TIME = (
    rf"(?:(?:오전|오후|저녁|새벽|아침|밤)\s*)?{_HOUR}\s*시"
    rf"(?:\s*(?:반|{_MINUTE}\s*분))?(?!\s*[0-9]+\s*분)"
    rf"|{_HOUR}:[0-5][0-9](?![0-9:])"
)
type TriggerKind = Literal["date", "time", "duration", "honorific"]
_RULES: tuple[tuple[TriggerKind, re.Pattern[str]], ...] = (
    ("date", re.compile(_LEFT + "(?:" + _DATE + ")" + _RIGHT)),
    ("time", re.compile(_LEFT + "(?:" + _TIME + ")" + _RIGHT)),
    (
        "duration",
        re.compile(
            _LEFT
            + r"(?:"
            + _NUMBER
            + r"\s*(?:개월|시간|달|일|주|년|분)|일주일|이틀|하루)"
            + _RIGHT
        ),
    ),
    (
        "honorific",
        re.compile(
            _LEFT
            + r"(?:[가-힣]{2,5}(?:\s*님|씨)|(?:팀장|과장|부장|대리|사장|대표|선생|교수|박사)\s*님)"
            + _RIGHT
        ),
    ),
)
# Only INTERNAL numeric dots are protected. A date's final dot followed by
# whitespace/end is a sentence terminator, just like a version's final dot.
_NUMERIC = re.compile(
    r"(?<![0-9])(?:[0-9]{4}\.\s*[0-9]{1,2}\.\s*[0-9]{1,2}|[0-9]+(?:\.[0-9]+)+)"
)
_BREAK = re.compile(r"[.!?。？！…]+[\"'”’」]?|\r?\n")
_BAD_PREFIXES = re.compile(
    r"오늘날|내일모레|내일림|(?:이번|다음|지난)\s*주스|[월화수목금토일]요일기"
    r"|[0-9]+\s*(?:시스템|월드)|하루살이"
)
_COMMON_NOUNS = {"고객님", "회원님", "하느님", "마음씨", "아저씨", "아가씨"}
_DAY_TOKEN = re.compile(r"([0-9]{1,2})\s*일")
_DURATION_SUFFIX = re.compile(r"\s*(?:동안|후|전|간|뒤|정도)")
_EXPLICIT_PREFIX = re.compile(r"[0-9]+\s*(?:월|시)\s*$")


def _excluded(kind: TriggerKind, match: re.Match[str], text: str) -> bool:
    if _BAD_PREFIXES.match(text, match.start()):
        return True
    if kind == "time" and text.startswith("스템", match.end()):
        return True
    if kind == "honorific":
        if "".join(match.group().split()) in _COMMON_NOUNS:
            return True
        if (match.group().endswith("씨") and text.startswith("앗", match.end())) or (
            match.group().endswith("님") and text.startswith("비", match.end())
        ):
            return True
    if kind in {"date", "duration"}:
        # A bounded lookbehind avoids copying/rescanning an entire long sentence
        # for every match. Numeric date/time components are not standalone days
        # or durations, including when their containing expression is invalid.
        prefix = text[max(0, match.start() - 64) : match.start()]
        if _EXPLICIT_PREFIX.search(prefix):
            return True
        day = _DAY_TOKEN.fullmatch(match.group())
        if day and 1 <= int(day[1]) <= 31:
            duration = _DURATION_SUFFIX.match(text, match.end()) is not None
            return (kind == "date" and duration) or (
                kind == "duration" and not duration
            )
    return False


class Refiner(Protocol):
    def select(self, thread: Thread) -> list[Candidate]: ...


@dataclass(frozen=True)
class _Sentence:
    start: int
    end: int


def _sentences(thread: Thread) -> list[_Sentence]:
    text = thread.text
    protected = [(m.start(), m.end()) for m in _NUMERIC.finditer(text)]
    cuts = {0, len(text)}
    protected_index = 0
    for match in _BREAK.finditer(text):
        while (
            protected_index < len(protected)
            and protected[protected_index][1] <= match.start()
        ):
            protected_index += 1
        if (
            protected_index == len(protected)
            or match.start() < protected[protected_index][0]
        ):
            cuts.add(match.end())
    for segment in thread.segments:
        cuts.update((segment.start_char, segment.end_char))
    edges = sorted(cuts)
    sentences = []
    for start, end in zip(edges, edges[1:], strict=False):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            sentences.append(_Sentence(start, end))
    return sentences


def _segments(thread: Thread, start: int, end: int) -> list[Segment]:
    return [s for s in thread.segments if s.start_char < end and start < s.end_char]


class RuleRefiner:
    """Select only triggered sentences plus configured context.

    No signals returns []; there is no whole-Thread fallback. Reference time is
    the first overlapping trigger Segment's non-null occurred_at, then Thread's,
    then null. Original offsets/time zones survive; no wall clock is consulted.
    Matching is lexical, so impossible calendar days and semantic ambiguity are
    left to extraction. All Segment boundaries remain sentence boundaries even
    when punctuation or speaker labels are absent.
    """

    def __init__(self, config: RefinerConfig | None = None) -> None:
        self.config = config or RefinerConfig()

    def select(self, thread: Thread) -> list[Candidate]:
        sentences = _sentences(thread)
        windows: list[tuple[int, int, list[CandidateTrigger]]] = []
        for index, sentence in enumerate(sentences):
            text = thread.text[sentence.start : sentence.end]
            triggers = []
            for kind, pattern in _RULES:
                for match in pattern.finditer(text):
                    if _excluded(kind, match, text):
                        continue
                    start, end = (
                        sentence.start + match.start(),
                        sentence.start + match.end(),
                    )
                    segments = _segments(thread, start, end)
                    reference = next(
                        (s.occurred_at for s in segments if s.occurred_at is not None),
                        thread.occurred_at,
                    )
                    triggers.append(
                        CandidateTrigger(
                            kind=kind,
                            start_char=start,
                            end_char=end,
                            sentence_index=index,
                            segment_ids=[s.segment_id for s in segments],
                            reference_at=reference,
                        )
                    )
            if not triggers:
                continue
            triggers.sort(key=lambda t: (t.start_char, t.end_char, t.kind))
            left = max(0, index - self.config.before)
            right = min(len(sentences), index + self.config.after + 1)
            if windows and left < windows[-1][1]:
                previous_left, previous_right, previous_triggers = windows[-1]
                previous_triggers.extend(triggers)
                windows[-1] = (
                    previous_left,
                    max(previous_right, right),
                    previous_triggers,
                )
            else:
                windows.append((left, right, triggers))
        candidates = []
        for left, right, triggers in windows:
            start, end = sentences[left].start, sentences[right - 1].end
            candidate = Candidate(
                source_id=thread.source_id,
                segment_ids=[s.segment_id for s in _segments(thread, start, end)],
                start_char=start,
                end_char=end,
                context=thread.text[start:end],
                start_sentence=left,
                end_sentence=right,
                triggers=triggers,
            )
            candidate.validate_thread(thread)
            candidates.append(candidate)
        return candidates
