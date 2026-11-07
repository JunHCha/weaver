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
_LEFT = rf"(?<![{_WORD}])"
# Permit Korean particles without matching prefixes of unrelated words.
_RIGHT = rf"(?=(?:(?:부터|까지|동안|에는|께서|께|쯤|경|마다|이면|에|은|는|이|가|을|를|과|와|도|만|의))?(?![{_WORD}]))"
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
    r"|오늘|어제|내일|모레|그제|(?:이번|다음|지난)\s*(?:주|달|해|월|년)"
    r"|[월화수목금토일]요일"
)
_TIME = (
    rf"(?:(?:오전|오후)\s*)?{_HOUR}\s*시"
    rf"(?:\s*(?:반|{_MINUTE}\s*분))?(?!\s*[0-9]+\s*분)"
    rf"|{_HOUR}:{_MINUTE}(?![0-9])"
)
type TriggerKind = Literal["date", "time", "duration", "honorific"]
_RULES: tuple[tuple[TriggerKind, re.Pattern[str]], ...] = (
    ("date", re.compile(_LEFT + "(?:" + _DATE + ")" + _RIGHT)),
    ("time", re.compile(_LEFT + "(?:" + _TIME + ")" + _RIGHT)),
    (
        "duration",
        re.compile(_LEFT + _NUMBER + r"\s*(?:개월|시간|달|일|주|년|분)" + _RIGHT),
    ),
    (
        "honorific",
        re.compile(
            _LEFT
            + r"(?:[가-힣]{2,5}\s*(?:님|씨)|(?:팀장|과장|부장|대리|사장|대표|선생|교수|박사)\s*님)"
            + _RIGHT
        ),
    ),
)
# Numeric punctuation belongs to the original expression, not a sentence break.
_NUMERIC = re.compile(r"[0-9]{4}\.\s*[0-9]{1,2}\.\s*[0-9]{1,2}\.?|[0-9]+\.[0-9]+")
_BREAK = re.compile(r"[.!?]+[\"'”’」]?|\r?\n")


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
    for match in _BREAK.finditer(text):
        if not any(start <= match.start() < end for start, end in protected):
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
                    # A minute/day inside an explicit time/date is not a second
                    # duration signal (nor a fallback for an invalid time/date).
                    if kind == "duration" and re.search(
                        r"[0-9]+\s*(?:월|시)\s*$", text[: match.start()]
                    ):
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
                windows[-1] = (
                    previous_left,
                    max(previous_right, right),
                    previous_triggers + triggers,
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
