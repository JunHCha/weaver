"""Versioned eval case format (#10) and its validating loader.

One JSON file per case under `server/evals/cases/<case_id>.json`. A case is
self-contained fake data: the raw source text, a fixed reference time/zone,
known entities with stable keys, and per-stage expectations. The loader turns a
case into domain models (Source, Thread, Candidate, EntityIndex, Extraction) so
a runner (#11) feeds each stage gold inputs and compares against expectations.
Format rules live in `server/evals/README.md`; this module enforces them.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StringConstraints, ValidationError, field_validator

from weaver.loom.call import CallParseError, parse_call
from weaver.loom.extract import normalize_name
from weaver.loom.kakao import KakaoParseError
from weaver.schema import Source, Thread
from weaver.schema.internal import Candidate, EntityIndex, Extraction, IndexedEntity
from weaver.schema.models import (
    Contract,
    EntityType,
    Hints,
    NonEmpty,
    ProposalEntity,
    RelationData,
    Timestamp,
    validate_proposal_references,
)
from weaver.spinner.reviser import (
    ReviseEntity,
    ReviseHints,
    Revision,
    RevisionError,
    RevisionSegment,
    revision_to_thread,
)
from weaver.spinner.text import text_to_thread

SCHEMA_VERSION = 1
CASES_DIR = Path(__file__).resolve().parents[3] / "evals" / "cases"
TAGS = frozenset(
    {
        # STT error kinds (call only)
        "stt_name",
        "stt_number",
        "stt_word",
        "stt_spacing",
        # Linking and identity
        "pseudonym",
        "homonym",
        "alias",
        "link_pending",
        "new_entity",
        # Time
        "relative_date",
        "absolute_date",
        "timezone",
        "missing_reference",
        # Content
        "request",
        "decision",
        "promise",
        "discussion",
        "cancel",
        "reschedule",
        "change",
        "chitchat",
        "missing_info",
        # Relations
        "relation_direction",
        "affiliation",
        "attendance",
        "follow_up",
        # Answer shape
        "empty",
        "multi_answer",
        "boundary",
    }
)
type Stage = Literal["spin", "refine", "extract"]
type CaseId = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
]
type Key = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*$")]
_ULID = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class DatasetError(ValueError):
    """Invalid eval case; the message names the case/file and the rule."""


class Reference(Contract):
    """Fixed reference instant; its offset must equal `timezone` at that instant."""

    at: Timestamp
    timezone: NonEmpty


class CaseInput(Contract):
    """`stage` is the earliest evaluated stage; text is always the raw original."""

    stage: Stage
    text: NonEmpty
    counterpart: NonEmpty | None = None


class KnownEntity(Contract):
    key: Key
    type: EntityType
    name: NonEmpty
    aliases: list[NonEmpty]


class Known(Contract):
    entities: list[KnownEntity] = Field(default_factory=list)


class QuoteAt(Contract):
    text: NonEmpty
    occurrence: int = Field(ge=1, strict=True)


type Quote = NonEmpty | QuoteAt


class ReviseKeep(Contract):
    """Text originals (kakao, share_text) are never revised."""

    policy: Literal["keep"]


class ReviseEdit(Contract):
    from_text: NonEmpty = Field(alias="from")
    to: Annotated[list[NonEmpty], Field(min_length=1)]


class ReviseCall(Contract):
    """Unlisted speech lines are expected unchanged; to[0] is the gold text."""

    policy: Literal["revise"]
    edits: list[ReviseEdit]


class RefineExpectation(Contract):
    """select: spans that must be covered (also the gold extract candidates).

    An empty select means no candidate is expected at all.
    """

    select: list[Quote]
    reject: list[Quote] = Field(default_factory=list)


class ExtractExpectation(Contract):
    """Any one alternative is correct; [] means nothing should be extracted."""

    alternatives: list[dict[str, Any]]


class Expected(Contract):
    revise: ReviseKeep | ReviseCall | None = None
    refine: RefineExpectation
    extract: ExtractExpectation


class Origin(Contract):
    kind: Literal["feedback"]
    versions: dict[str, str]


class EvalCase(Contract):
    schema_version: Literal[1]
    case_id: CaseId
    channel: Literal["call", "kakao", "share_text"]
    title: NonEmpty
    reference: Reference
    tags: list[str]
    input: CaseInput
    known: Known
    expected: Expected
    notes: NonEmpty | None = None
    origin: Origin | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value


@dataclass(frozen=True)
class LoadedCase:
    """A validated case as domain models; `thread` is the gold (revised) Thread."""

    case: EvalCase
    source: Source
    raw_thread: Thread
    thread: Thread
    candidates: list[Candidate]
    reject_spans: list[tuple[int, int]]
    known: EntityIndex
    entity_ids: dict[str, str]
    revise_hints: ReviseHints
    extractions: list[Extraction]


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic ULID-shaped ID so runs never differ by random IDs."""
    digest = hashlib.sha256(":".join(parts).encode()).digest()
    number = int.from_bytes(digest[:16]) >> 2  # 126 bits -> first char 0..3
    chars = []
    for _ in range(26):
        chars.append(_ULID[number & 31])
        number >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"


def _zone(case: EvalCase) -> ZoneInfo:
    try:
        return ZoneInfo(case.reference.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise DatasetError("unknown timezone") from None


def _check_offset(value: datetime, zone: ZoneInfo, what: str) -> None:
    if value.utcoffset() != value.astimezone(zone).utcoffset():
        raise DatasetError(f"{what} offset differs from case timezone")


def _check_shape(case: EvalCase) -> None:
    tags = case.tags
    if len(tags) != len(set(tags)):
        raise DatasetError("duplicate tag")
    if unknown := sorted(set(tags) - TAGS):
        raise DatasetError(f"unknown tags: {unknown}")
    if case.channel != "call" and any(t.startswith("stt_") for t in tags):
        raise DatasetError("stt tags apply only to call")
    revise = case.expected.revise
    if case.input.stage == "spin" and revise is None:
        raise DatasetError("spin stage requires expected.revise")
    if case.channel == "call":
        if isinstance(revise, ReviseKeep):
            raise DatasetError("call expects policy revise")
        stt = any(t.startswith("stt_") for t in tags)
        if isinstance(revise, ReviseCall) and stt != bool(revise.edits):
            raise DatasetError("stt tags and revise edits must agree")
    elif isinstance(revise, ReviseCall):
        raise DatasetError("text originals must keep their text")
    keys = [e.key for e in case.known.entities]
    if len(keys) != len(set(keys)):
        raise DatasetError("duplicate known entity key")
    alternatives = case.expected.extract.alternatives
    if ("empty" in tags) != (not alternatives):
        raise DatasetError("empty tag must match an empty alternatives list")
    multi = len(alternatives) > 1 or (
        isinstance(revise, ReviseCall) and any(len(e.to) > 1 for e in revise.edits)
    )
    if ("multi_answer" in tags) != multi:
        raise DatasetError("multi_answer tag must match multiple alternatives")


def _source(case: EvalCase) -> Source:
    capture = stable_id("cap", case.case_id)
    return Source(
        schema_version=1,
        source_id=stable_id("src", case.case_id),
        capture_id=capture,
        kind="text",
        channel=case.channel,
        captured_at=case.reference.at,
        occurred_at=case.reference.at,
        sha256=hashlib.sha256(case.input.text.encode()).hexdigest(),
        hints=Hints(counterpart=case.input.counterpart),
        payload_ref=f"text/{capture}.txt",
    )


def _parse(case: EvalCase, source: Source, zone: ZoneInfo) -> Thread:
    try:
        if case.channel == "call":
            return parse_call(source, case.input.text).thread
        return text_to_thread(source, case.input.text, timezone=zone)
    except (CallParseError, KakaoParseError, ValueError) as error:
        raise DatasetError(f"input does not parse: {error}") from None


def _revise(case: EvalCase, raw: Thread) -> Thread:
    revise = case.expected.revise
    if not isinstance(revise, ReviseCall):
        return raw
    texts = {s.segment_id: raw.text[s.start_char : s.end_char] for s in raw.segments}
    speech = [s.segment_id for s in raw.segments if s.speaker is not None]
    replaced: dict[str, str] = {}
    for edit in revise.edits:
        matches = [sid for sid in speech if texts[sid] == edit.from_text]
        if len(matches) != 1:
            raise DatasetError("revise edit must match exactly one speech line")
        if matches[0] in replaced:
            raise DatasetError("duplicate revise edit")
        replaced[matches[0]] = edit.to[0]
    revision = Revision(
        segments=[
            RevisionSegment(
                segment_id=s.segment_id,
                start=None if s.start_ms is None else s.start_ms / 1000,
                end=None if s.end_ms is None else s.end_ms / 1000,
                speaker=s.speaker,
                text=replaced.get(s.segment_id, texts[s.segment_id]),
            )
            for s in raw.segments
        ]
    )
    try:
        return revision_to_thread(revision, raw)
    except RevisionError as error:
        raise DatasetError(f"revise edits invalid: {error}") from None


def _span(text: str, quote: Quote) -> tuple[int, int]:
    needle, occurrence = (
        (quote, None) if isinstance(quote, str) else (quote.text, quote.occurrence)
    )
    starts = []
    start = text.find(needle)
    while start != -1:
        starts.append(start)
        start = text.find(needle, start + 1)
    if occurrence is None:
        if len(starts) != 1:
            raise DatasetError("quote must occur exactly once (or use occurrence)")
        return starts[0], starts[0] + len(needle)
    if occurrence > len(starts):
        raise DatasetError("quote occurrence not found")
    begin = starts[occurrence - 1]
    return begin, begin + len(needle)


def _candidates(
    case: EvalCase, thread: Thread
) -> tuple[list[Candidate], list[tuple[int, int]]]:
    spans = sorted(_span(thread.text, q) for q in case.expected.refine.select)
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        if start < end:
            raise DatasetError("select spans overlap")
    rejects = [_span(thread.text, q) for q in case.expected.refine.reject]
    for r_start, r_end in rejects:
        if any(r_start < end and start < r_end for start, end in spans):
            raise DatasetError("reject span overlaps a select span")
    candidates = [
        Candidate(
            source_id=thread.source_id,
            segment_ids=[
                s.segment_id
                for s in thread.segments
                if s.start_char < end and start < s.end_char
            ],
            start_char=start,
            end_char=end,
            context=thread.text[start:end],
        )
        for start, end in spans
    ]
    for candidate in candidates:
        candidate.validate_thread(thread)
    return candidates, rejects


def _substitute(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        if value.startswith("ent_"):
            raise DatasetError("use @key instead of a literal ent_ ID")
        if value.startswith("@"):
            if value[1:] not in ids:
                raise DatasetError(f"unknown entity key {value}")
            return ids[value[1:]]
        return value
    if isinstance(value, list):
        return [_substitute(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, ids) for k, v in value.items()}
    return value


def _excerpts(extraction: Extraction) -> list[str]:
    items: list[Any] = [
        *extraction.entities,
        *extraction.schedules,
        *extraction.relations,
        *extraction.entry_drafts,
    ]
    return [extraction.thread_excerpt, *(i.thread_excerpt for i in items)]


def _timestamps(extraction: Extraction) -> list[datetime]:
    found: list[datetime] = []
    for schedule in extraction.schedules:
        found += [schedule.start, *([schedule.end] if schedule.end else [])]
    for draft in extraction.entry_drafts:
        found.append(draft.occurred_at)
        if draft.request is not None and draft.request.due_at is not None:
            found.append(draft.request.due_at)
    return found


def _extraction(
    raw: dict[str, Any],
    ids: dict[str, str],
    thread: Thread,
    candidates: list[Candidate],
    zone: ZoneInfo,
) -> Extraction:
    try:
        extraction = Extraction.model_validate(_substitute(raw, ids))
        validate_proposal_references(
            [
                ProposalEntity.model_validate(e.model_dump())
                for e in extraction.entities
            ],
            extraction.schedules,
            [
                RelationData.model_validate(r.model_dump(by_alias=True))
                for r in extraction.relations
            ],
            extraction.entry_drafts,
        )
    except (ValidationError, ValueError) as error:
        if isinstance(error, DatasetError):
            raise
        raise DatasetError(f"invalid expected extraction: {error}") from None
    contexts = [c.context for c in candidates]
    for text in _excerpts(extraction):
        if not any(text in context for context in contexts):
            raise DatasetError("thread_excerpt must come from a selected span")
    haystack = normalize_name(thread.text)
    for entity in extraction.entities:
        if entity.entity_id is None and normalize_name(entity.name) not in haystack:
            raise DatasetError("new entity name must occur in the input")
    for value in _timestamps(extraction):
        _check_offset(value, zone, "expected timestamp")
    return extraction


def validate_case(data: object) -> LoadedCase:
    """Validate one case object and build its gold stage inputs/expectations."""
    if not isinstance(data, dict) or not data:
        raise DatasetError("case must be a nonempty JSON object")
    try:
        case = EvalCase.model_validate(data)
    except ValidationError as error:
        raise DatasetError(f"invalid case: {error}") from None
    _check_shape(case)
    zone = _zone(case)
    _check_offset(case.reference.at, zone, "reference.at")
    source = _source(case)
    raw = _parse(case, source, zone)
    thread = _revise(case, raw)
    candidates, rejects = _candidates(case, thread)
    alternatives = case.expected.extract.alternatives
    if alternatives and not candidates:
        raise DatasetError("expected facts require a selected span")
    ids = {e.key: stable_id("ent", case.case_id, e.key) for e in case.known.entities}
    known = EntityIndex(
        entities=[
            IndexedEntity(
                entity_id=ids[e.key], type=e.type, name=e.name, aliases=e.aliases
            )
            for e in case.known.entities
        ]
    )
    hints = ReviseHints(
        entities=[
            ReviseEntity(entity_id=e.entity_id, name=e.name, aliases=e.aliases)
            for e in known.entities
        ],
        counterpart=case.input.counterpart,
    )
    extractions = [
        _extraction(alt, ids, thread, candidates, zone) for alt in alternatives
    ]
    return LoadedCase(
        case, source, raw, thread, candidates, rejects, known, ids, hints, extractions
    )


def load_case(path: Path) -> LoadedCase:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DatasetError(f"{path.name}: unreadable JSON") from None
    try:
        loaded = validate_case(data)
    except DatasetError as error:
        raise DatasetError(f"{path.name}: {error}") from None
    if path.name != f"{loaded.case.case_id}.json":
        raise DatasetError(f"{path.name}: file name must be <case_id>.json")
    return loaded


def load_dataset(directory: Path = CASES_DIR) -> list[LoadedCase]:
    """Load every `*.json` case, sorted by file name, with unique case IDs."""
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not paths:
        raise DatasetError("no cases found")
    cases = [load_case(p) for p in paths]
    ids = Counter(c.case.case_id for c in cases)
    if duplicates := sorted(i for i, n in ids.items() if n > 1):
        raise DatasetError(f"duplicate case_id: {duplicates}")
    return cases


def summarize(cases: list[LoadedCase]) -> dict[str, Any]:
    """Counts by channel, tag, answer shape, entry kind and relation type."""
    channel: Counter[str] = Counter()
    tag: Counter[str] = Counter()
    answer: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    relations: Counter[str] = Counter()
    for loaded in cases:
        channel[loaded.case.channel] += 1
        tag.update(loaded.case.tags)
        if not loaded.extractions:
            answer["empty"] += 1
        if len(loaded.extractions) > 1:
            answer["multiple"] += 1
        first = loaded.extractions[:1]
        for extraction in first:
            kinds.update(d.kind for d in extraction.entry_drafts)
            relations.update(r.type for r in extraction.relations)
            if any(
                item.ask is not None
                for item in [*extraction.entities, *extraction.entry_drafts]
            ):
                answer["ask"] += 1
            if any(e.entity_id is not None for e in extraction.entities):
                answer["linked"] += 1
            if any(e.entity_id is None for e in extraction.entities):
                answer["new_entity"] += 1
    return {
        "total": len(cases),
        "channel": dict(channel),
        "tag": dict(tag),
        "answer": dict(answer),
        "entry_kind": dict(kinds),
        "relation_type": dict(relations),
    }
