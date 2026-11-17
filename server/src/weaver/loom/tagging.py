"""Shared bounded dictionary input and deterministic tag validation."""

import json
import logging
import re
import unicodedata
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from weaver.context import DictionarySnapshot, Kind, _key
from weaver.llm.errors import SchemaMismatch
from weaver.schema import Thread
from weaver.schema.models import NonEmpty

logger = logging.getLogger(__name__)

TAG_RULES = (
    "각 업무 Thread의 tags에는 영역(area) 정확히 하나를 포함하고, 관련된 기존 프로젝트·"
    "언급되거나 관여한 사람(요청자·담당자·상대방)·조직·주제의 사전 표준 이름을 모두 붙인다."
    " 기존 영역을 우선하고 맞는 영역이 없을 때만 적고 넓은 영역을 new_entries로 만든다."
    " area는 PARA의 지속 책임, topic은 PARA resource다."
    " 프로젝트는 기존 사전 항목이 명확히 맞을 때만 쓰며 새 project는 만들지 않는다."
    " Wiki의 자동 project는 사람이 편집하기 전까지 태그 후보가 아니다."
    " 사람 표준 이름에 직함·소속·메모를 넣지 않는다."
    " dictionary의 aliases는 표준 name으로 바꾸고 기존 태그도 사전으로 확인한다."
    " 사전에 없는 이름은 new_entries에 name, kind(person/organization/area/topic),"
    " 선택적 aliases와 함께 반환한 경우에만 쓴다. TODO나 확인 같은 작업 상태는 태그가 아니다."
    " new_entries.aliases는 실제로 같은 대상을 부르는 다른 이름"
    "(약칭·영문/한글 표기·별명)만이다. 오인식·오타·띄어쓰기·대소문자 차이는 별칭이 아니다."
    " counterpart가 있으면 그 표준 이름도 반드시 tags에 포함한다."
)

FACT_RULES = (
    " 추출 대상은 업무 사실(요청·결정·일정·기록)만이다."
    " 녹취 해석(오인식·용어 뜻), 사람 동일성 확인, 불확실함·미확인 진술,"
    " 검토 질문과 답 자체는 Thread로 만들지 않는다."
    " 검토 답은 해당 업무 사실의 이름·용어·내용을 고치는 데만 쓴다."
)

# UTF-8 bytes conservatively bound tokens without depending on a provider tokenizer.
TAG_CONTEXT_TOKEN_BUDGET = 8000
EXISTING_TAG_TOKEN_BUDGET = 2000
MAX_NEW_ENTRIES = 50
MAX_LABEL_LENGTH = 120
type EntryLabel = Annotated[NonEmpty, StringConstraints(max_length=MAX_LABEL_LENGTH)]


class NewEntry(BaseModel):
    """A canonical tag proposed together with its dictionary classification."""

    model_config = ConfigDict(extra="forbid")
    name: EntryLabel
    kind: Kind
    aliases: list[EntryLabel] = Field(default_factory=list, max_length=20)


def tag_context(
    snapshot: DictionarySnapshot, script: str, threads: list[Thread]
) -> dict[str, Any]:
    """Bound prompt hints; full snapshot still validates every returned tag."""
    excluded = {
        e.name
        for e in snapshot.entries
        if e.kind == "project" and e.source == "wiki" and not e.human_edited
    }
    recent_tags = list(
        dict.fromkeys(
            unicodedata.normalize("NFC", tag)
            for thread in sorted(threads, key=lambda t: t.seq, reverse=True)
            for tag in thread.tags
            if not ((entry := snapshot.resolve(tag)) and entry.name in excluded)
        )
    )
    existing: list[str] = []
    cost = len(json.dumps({"dictionary": [], "existing_tags": []}).encode("utf-8"))
    for tag in recent_tags:
        size = len(json.dumps(tag, ensure_ascii=False).encode("utf-8")) + 2
        if cost + size <= EXISTING_TAG_TOKEN_BUDGET:
            existing.append(tag)
            cost += size
    snapshot = tag_snapshot(snapshot)
    candidates = snapshot.candidates(script, limit=30)
    recent_entries = [entry for tag in recent_tags if (entry := snapshot.resolve(tag))]
    ranked = [
        *(entry for entry in snapshot.entries if entry.kind == "area"),
        *candidates,
        *recent_entries,
        *snapshot.entries,
    ]
    dictionary: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in ranked:
        if entry.name in seen:
            continue
        seen.add(entry.name)
        hint = {"name": entry.name, "kind": entry.kind, "aliases": entry.aliases}
        size = len(json.dumps(hint, ensure_ascii=False).encode("utf-8")) + 2
        if cost + size <= TAG_CONTEXT_TOKEN_BUDGET:
            dictionary.append(hint)
            cost += size
    return {"dictionary": dictionary, "existing_tags": existing}


def clean_name(name: str, kind: str) -> bool:
    """Conservative structural guard; the model removes titles and affiliations."""
    name = unicodedata.normalize("NFC", name)
    if not name.strip() or any(c in name for c in "()[]\n:"):
        return False
    if re.search(
        r"(?:팀장|수석|프로|대표|부장|과장|대리|님)$|수정\s*필요|확인\s*필요|미확인",
        name,
    ):
        return False
    if kind == "person":
        if re.search(r"[가-힣]", name) and any(c.isspace() for c in name):
            return False
        return bool(re.fullmatch(r"[가-힣]{2,}|[^\W\d_]+(?:[ .'-][^\W\d_]+)*", name))
    return True


def proposals(
    dictionary: DictionarySnapshot, entries: list[NewEntry]
) -> tuple[list[NewEntry], dict[str, str]]:
    new_entries: list[NewEntry] = []
    proposed_names: dict[str, str] = {}
    for entry in entries:
        if entry.kind == "project":
            logger.warning("dropped automatic project proposal")
            continue
        if entry.kind in ("person", "organization") and not clean_name(
            entry.name, entry.kind
        ):
            logger.warning("dropped invalid dictionary name")
            continue
        canonical = unicodedata.normalize("NFC", entry.name)
        if dictionary.resolve(canonical):
            continue
        key = _key(canonical)
        if key in proposed_names:
            continue
        aliases = [unicodedata.normalize("NFC", a) for a in entry.aliases]
        safe_aliases = [
            a
            for a in aliases
            if _key(a) != key
            and not dictionary.resolve(a)
            and _key(a) not in proposed_names
        ]
        if len(safe_aliases) < len(aliases):
            logger.warning("dropped conflicting dictionary aliases")
        aliases = safe_aliases
        new_entries.append(NewEntry(name=canonical, kind=entry.kind, aliases=aliases))
        for label in (canonical, *aliases):
            proposed_names[_key(label)] = canonical
    return new_entries, proposed_names


def canonical_tags(
    dictionary: DictionarySnapshot, proposed_names: dict[str, str], values: list[str]
) -> list[str]:
    tags = []
    for tag in values:
        entry = dictionary.resolve(tag)
        canonical = entry.name if entry else proposed_names.get(_key(tag))
        if canonical is None:
            logger.warning("dropped unresolved approval tag")
        elif canonical not in tags:
            tags.append(canonical)
    return tags


def tag_snapshot(snapshot: DictionarySnapshot) -> DictionarySnapshot:
    """Incident-style generated project pages are not promoted projects (#149)."""
    return DictionarySnapshot(
        [
            entry
            for entry in snapshot.entries
            if entry.kind != "project" or entry.source != "wiki" or entry.human_edited
        ]
    )


def business_tags(
    dictionary: DictionarySnapshot,
    additions: list[NewEntry],
    names: dict[str, str],
    values: list[str],
) -> list[str]:
    tags = canonical_tags(dictionary, names, values)
    kinds = {e.name: e.kind for e in dictionary.entries} | {
        e.name: e.kind for e in additions
    }
    if sum(kinds[tag] == "area" for tag in tags) != 1:
        logger.warning("invalid business area tag count")
        raise SchemaMismatch()
    return tags


def counterpart_name(
    dictionary: DictionarySnapshot,
    additions: list[NewEntry],
    names: dict[str, str],
    value: str | None,
) -> str | None:
    if value is None:
        return None
    entry = dictionary.resolve(value)
    canonical = entry.name if entry else names.get(_key(value))
    kinds = {e.name: e.kind for e in dictionary.entries} | {
        e.name: e.kind for e in additions
    }
    if canonical and kinds.get(canonical) in ("person", "organization"):
        return canonical
    logger.warning("dropped invalid business counterpart")
    return None
