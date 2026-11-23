"""Shared bounded dictionary input and deterministic tag validation."""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from weaver.context import DictionarySnapshot, Kind, _key, _score
from weaver.context.tree import UNCLASSIFIED, node_kind, root_of
from weaver.llm.errors import SchemaMismatch
from weaver.schema import Thread
from weaver.schema.models import NonEmpty

logger = logging.getLogger(__name__)

LEGACY_TAG_RULES = (
    "각 업무 Thread의 tags에는 영역(area) 정확히 하나를 포함하고, 관련된 기존 프로젝트·"
    "언급되거나 관여한 사람(요청자·담당자·상대방)·조직·주제의 사전 표준 이름을 모두 붙인다."
    " 기존 영역을 우선하고 맞는 영역이 없을 때만 적고 넓은 영역을 new_entries로 만든다."
    " area는 PARA의 지속 책임, topic은 PARA resource다."
    " 프로젝트는 기존 사전 항목이 명확히 맞을 때만 쓰며 새 project는 만들지 않는다."
    " Wiki에서 승격된 활성 프로젝트만 자동 project 태그 후보가 된다."
    " 사람 표준 이름에 직함·소속·메모를 넣지 않는다."
    " dictionary의 aliases는 표준 name으로 바꾸고 기존 태그도 사전으로 확인한다."
    " 사전에 없는 이름은 new_entries에 name, kind(person/organization/area/topic),"
    " 선택적 aliases와 함께 반환한 경우에만 쓴다. TODO나 확인 같은 작업 상태는 태그가 아니다."
    " new_entries.aliases는 실제로 같은 대상을 부르는 다른 이름"
    "(약칭·영문/한글 표기·별명)만이다. 오인식·오타·띄어쓰기·대소문자 차이는 별칭이 아니다."
    " counterpart가 있으면 그 표준 이름도 반드시 tags에 포함한다."
)

TAG_RULES = (
    "각 업무 Thread의 tags에는 paths에 있는 대상 경로 1~2개와, 해당하면"
    " `<생활권>/project/…` 경로 0~1개를 쓴다."
    " 경로는 제공된 것만 그대로 쓰고 새 경로를 만들지 않는다."
    " 맞는 경로가 없으면 가장 가까운 상위 경로를 쓴다."
    " 어디에도 맞지 않으면 해당 생활권의 `<생활권>/미분류` 경로를 쓴다(paths에 있다)."
    " 사람·조직은 dictionary의 표준 이름으로 붙이고"
    " 사전에 없는 사람·조직만 new_entries(person/organization)로 제안한다."
    " 사람 표준 이름에 직함·소속·메모를 넣지 않는다."
    " new_entries.aliases는 실제로 같은 대상을 부르는 다른 이름"
    "(약칭·영문/한글 표기·별명)만이다. 오인식·오타·띄어쓰기·대소문자 차이는 별칭이 아니다."
    " 그 밖의 자유 단어는 태그가 아니다. TODO나 확인 같은 작업 상태도 태그가 아니다."
    " counterpart가 있으면 그 표준 이름도 반드시 tags에 포함한다."
)


def tag_rules(snapshot: DictionarySnapshot) -> str:
    return TAG_RULES if snapshot.has_tree else LEGACY_TAG_RULES


FACT_RULES = (
    " 추출 대상은 업무 사실(요청·결정·일정·기록)만이다."
    " 녹취 해석(오인식·용어 뜻), 사람 동일성 확인, 불확실함·미확인 진술,"
    " 검토 질문과 답 자체는 Thread로 만들지 않는다."
    " 검토 답은 해당 업무 사실의 이름·용어·내용을 고치는 데만 쓴다."
)

CONTENT_RULES = (
    "서로 다른 사실(요청·결정·일정·기록)은 각각 별도 Thread로 유지한다."
    " 서로 다른 사실이나 종류를 한 Thread로 합치지 않고, 줄이려고 결정·기록을 빼지 않는다."
    " '한 줄'은 Thread마다 적용되는 content 길이 규칙이다."
    " 각 Thread의 content는 개조식 한 줄로 쓰고 명사형으로 끝낸다"
    "(예: '서버 정기점검 방문 일정 확정', '방화벽 정책 변경 요청')."
    " '~했다/~한다/~요' 같은 문장 종결과 마침표를 쓰지 않는다."
    " 날짜·시각·기한은 content에 쓰지 않고 schedule/deadline 값에 둔다."
)
FACT_RULES += " " + CONTENT_RULES

# UTF-8 bytes conservatively bound tokens without depending on a provider tokenizer.
TAG_CONTEXT_TOKEN_BUDGET = 8000
EXISTING_TAG_TOKEN_BUDGET = 2000
PATH_BUDGET = 4000
MAX_SUBJECTS = 2
MAX_PROJECTS = 1
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
        if e.archived
        or e.promotion_reverted
        or (
            e.kind == "project"
            and e.source == "wiki"
            and not e.human_edited
            and not e.promoted
        )
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
    ranked = [entry for entry in ranked if entry.source != "tree"]
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
    result: dict[str, Any] = {"dictionary": dictionary, "existing_tags": existing}
    if snapshot.has_tree:
        result["paths"] = _paths(snapshot, script)
    return result


def _paths(snapshot: DictionarySnapshot, script: str) -> list[str]:
    """Bounded tree paths: script matches with ancestors, areas, live projects."""
    nodes = {e.name: e for e in snapshot.entries if e.source == "tree"}
    if not nodes:
        return []
    # Full paths never resemble prose, so match on each node's own last segment.
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", script))
    scored = sorted(
        (score, name)
        for name in nodes
        if "/" in name and (score := _score(name.rsplit("/", 1)[1], tokens)) is not None
    )
    ordered: list[str] = []
    for _, name in scored[:30]:
        parts = name.split("/")
        ordered.extend(
            "/".join(parts[:end])
            for end in range(1, len(parts) + 1)
            if "/".join(parts[:end]) in nodes
        )
    ordered.extend(n for n, e in nodes.items() if e.kind == "area")
    # Fallback targets must always be offered, so they lead the budgeted tail.
    unclassified = [
        n for n in nodes if n.count("/") == 1 and n.endswith(f"/{UNCLASSIFIED}")
    ]
    ordered = [*unclassified, *ordered]
    ordered.extend(
        n
        for n, e in nodes.items()
        if e.kind == "project" and not e.archived and not e.promotion_reverted
    )
    paths: list[str] = []
    cost = 2
    for name in dict.fromkeys(ordered):
        size = len(json.dumps(name, ensure_ascii=False).encode("utf-8")) + 2
        if cost + size <= PATH_BUDGET:
            paths.append(name)
            cost += size
    return paths


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
    tree = dictionary.has_tree
    for entry in entries:
        if tree and entry.kind not in ("person", "organization"):
            logger.warning("dropped tree node proposal")
            continue
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
    """Only active promoted or curated projects are extraction candidates."""
    return DictionarySnapshot(
        [
            entry
            for entry in snapshot.entries
            if not entry.archived
            and not entry.promotion_reverted
            and (
                entry.kind != "project"
                or entry.source != "wiki"
                or entry.human_edited
                or entry.promoted
            )
        ]
    )


@dataclass(frozen=True)
class TagDecision:
    tags: list[str]
    unknown_paths: list[str]
    fallback: str | None


def business_tags(
    dictionary: DictionarySnapshot,
    additions: list[NewEntry],
    names: dict[str, str],
    values: list[str],
    *,
    default_sphere: str = "회사",
) -> TagDecision:
    if not dictionary.has_tree:
        tags = canonical_tags(dictionary, names, values)
        kinds = {e.name: e.kind for e in dictionary.entries} | {
            e.name: e.kind for e in additions
        }
        if sum(kinds[tag] == "area" for tag in tags) != 1:
            logger.warning("invalid business area tag count")
            raise SchemaMismatch()
        return TagDecision(tags, [], None)
    added = {e.name: e.kind for e in additions}
    subjects: list[str] = []
    projects: list[str] = []
    mentions: list[str] = []
    unknown: list[str] = []
    hinted_roots: list[str] = []
    for value in values:
        entry = dictionary.resolve(value)
        name = entry.name if entry else names.get(_key(value))
        if entry is not None and entry.source == "tree":
            kind = node_kind(entry.name)
            depth = len(entry.name.split("/"))
            if kind == "project":
                _append(projects, entry.name)
            elif depth > 1 and not (kind == "topic" and depth == 2):
                _append(subjects, entry.name)
            else:
                logger.warning("dropped namespace tag")
                _append(hinted_roots, root_of(entry.name))
        elif name is not None and (
            (entry.kind if entry else added.get(name)) in ("person", "organization")
        ):
            _append(mentions, name)
        elif (
            entry is None
            and "/" in value
            and (root := unicodedata.normalize("NFC", root_of(value)))
            and (known := dictionary.resolve(root))
            and known.source == "tree"
        ):
            _append(unknown, unicodedata.normalize("NFC", value))
        else:
            logger.warning("dropped free tag")
    if len(subjects) > MAX_SUBJECTS:
        logger.warning("dropped extra subject paths")
    if len(projects) > MAX_PROJECTS:
        logger.warning("dropped extra project paths")
    subjects, projects = subjects[:MAX_SUBJECTS], projects[:MAX_PROJECTS]
    fallback = None
    if not subjects:
        root = (
            root_of(next(iter([*unknown, *projects]), ""))
            or next(iter(hinted_roots), "")
            or default_sphere
        )
        fallback = f"{root}/미분류"
        subjects = [fallback]
    return TagDecision([*subjects, *projects, *mentions], unknown, fallback)


def _append(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


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
