"""Shared bounded dictionary input and deterministic tag validation."""

import json
import logging
import re
import unicodedata
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from weaver.context import DictionarySnapshot, Kind, _key
from weaver.schema import Thread
from weaver.schema.models import NonEmpty

logger = logging.getLogger(__name__)

TAG_RULES = (
    " 각 Thread의 tags에는 관련 프로젝트, 언급되거나 관여한 사람"
    "(요청자·담당자·상대방), 조직, 관련 주제를 모두 사전 표준 이름으로 붙인다."
    " 기록 주인(화자 본인)은 태그하지 않는다. contact_name은 통화 상대다."
    " 사람 표준 이름에 직함·소속·메모를 넣지 않는다."
    " dictionary의 aliases는 표준 name으로 바꾼다. 기존 태그도 사전으로 확인한다."
    " 사전에 없는 이름은 new_entries에 name, kind(person/organization/project/topic),"
    " 선택적 aliases와 함께 반환한 경우에만 태그로 쓸 수 있다."
    " TODO나 확인 같은 작업 상태를 태그로 만들지 않는다."
    " new_entries.aliases는 실제로 같은 대상을 부르는 다른 이름"
    "(약칭·영문/한글 표기·별명)만이다. 오인식·오타·띄어쓰기·대소문자 차이는 별칭이 아니다."
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
    recent_tags = list(
        dict.fromkeys(
            unicodedata.normalize("NFC", tag)
            for thread in sorted(threads, key=lambda t: t.seq, reverse=True)
            for tag in thread.tags
        )
    )
    existing: list[str] = []
    cost = len(json.dumps({"dictionary": [], "existing_tags": []}).encode("utf-8"))
    for tag in recent_tags:
        size = len(json.dumps(tag, ensure_ascii=False).encode("utf-8")) + 2
        if cost + size <= EXISTING_TAG_TOKEN_BUDGET:
            existing.append(tag)
            cost += size
    candidates = snapshot.candidates(script, limit=30)
    recent_entries = [entry for tag in recent_tags if (entry := snapshot.resolve(tag))]
    ranked = [*candidates, *recent_entries, *snapshot.entries]
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
