"""Deterministic Markdown templates for AI-folder notes (#7).

Every note starts with flat YAML properties (Obsidian shows nested ones poorly)
and ends its frontmatter with ``weaver_content_sha256``: the SHA-256 of the file
bytes without that one line. A file whose embedded hash still matches is an
untouched Weaver output; anything else is human-owned and is never overwritten.
OutputWatcher (#8) can apply the same check to copies, which keep the line.

The same inputs always produce the same bytes: no clocks, sorted lists only.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from weaver.schema import EntityState, Entry
from weaver.schema.internal import IndexedEntity

SCHEMA_VERSION = 1
HASH_KEY = "weaver_content_sha256"
NOTICE = (
    "> [!info] 위버가 쓰는 노트입니다. 직접 고치면 위버가 더 이상 갱신하지 않습니다. "
    "고칠 내용은 복사해서 산출물로 만드세요."
)
_HASH_LINE = re.compile(rf"^{HASH_KEY}: ([0-9a-f]{{64}})\n", re.MULTILINE)
_LINK_UNSAFE = str.maketrans(
    {"|": "/", "[": "(", "]": ")", "#": "＃", "^": "＾", ":": "：", "%": "％"}
)


@dataclass(frozen=True)
class RelationLink:
    """An approved relation; endpoints are ent_/evt_ IDs or a src_ fallback."""

    type: str
    from_ids: tuple[str, ...]
    to_ids: tuple[str, ...]
    reason: str | None
    source_id: str


@dataclass(frozen=True)
class Note:
    note_id: str
    kind: str  # "entity" | "entry"
    data: bytes
    source_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]
    entry_ids: tuple[str, ...]
    sha256: str


def one_line(text: str) -> str:
    return " ".join(text.split())


def plain(text: str) -> str:
    """One line of free text that cannot form links, tags, comments or
    Dataview fields: CommonMark backslash escapes render the same characters.
    """
    escaped = one_line(text).replace("\\", "\\\\")
    for char in "[]#%":
        escaped = escaped.replace(char, "\\" + char)
    return escaped.replace("::", "\\:\\:")


def value_text(value: JsonValue) -> str:
    if value is None:
        return "없음"
    if isinstance(value, str):
        return plain(value)
    return plain(json.dumps(value, ensure_ascii=False, sort_keys=True))


def entity_name(entity_id: str, names: Mapping[str, IndexedEntity]) -> str:
    known = names.get(entity_id)
    return one_line(known.name).translate(_LINK_UNSAFE) if known else entity_id


def link(target: str, names: Mapping[str, IndexedEntity]) -> str:
    """ID-based wikilink; the display text may go stale, the target never."""
    if target.startswith("ent_"):
        return f"[[{target}|{entity_name(target, names)}]]"
    if target.startswith("evt_"):
        return f"[[{target}]]"
    return target  # src_/output: sources live outside the vault


def links(targets: Iterable[str], names: Mapping[str, IndexedEntity]) -> str:
    return ", ".join(link(target, names) for target in targets)


def local_date(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).date().isoformat()


def summary(item: Entry) -> str:
    if item.request is not None:
        return plain(item.request.content)
    if item.field is not None:
        return f"{plain(item.field)} = {value_text(item.value)}"
    return value_text(item.value)


def entry_status(item: Entry, retracted: set[str], superseded: set[str]) -> str:
    if item.retracts is not None:
        return "철회 기록"
    if item.entry_id in retracted:
        return "철회됨"
    if item.entry_id in superseded:
        return "대체됨"
    return "유효"


def statuses(entries: Sequence[Entry]) -> tuple[set[str], set[str]]:
    """Same activity rule as Fabric's fold_state over one entity's snapshot."""
    retracted = {e.retracts for e in entries if e.retracts is not None}
    superseded = {
        e.supersedes
        for e in entries
        if e.entry_id not in retracted and e.supersedes is not None
    }
    return retracted, superseded


def _yaml(key: str, value: str | int | Sequence[str]) -> list[str]:
    # JSON strings are valid YAML scalars and keep any name safely quoted.
    if isinstance(value, int):
        return [f"{key}: {value}"]
    if isinstance(value, str):
        return [f"{key}: {json.dumps(value, ensure_ascii=False)}"]
    if not value:
        return [f"{key}: []"]
    return [f"{key}:", *(f"  - {json.dumps(v, ensure_ascii=False)}" for v in value)]


def _finish(
    note_id: str,
    kind: str,
    properties: list[tuple[str, str | int | Sequence[str]]],
    body: list[str],
    *,
    source_ids: Iterable[str],
    entity_ids: Iterable[str],
    entry_ids: Iterable[str],
) -> Note:
    sources = tuple(sorted(set(source_ids)))
    entities = tuple(sorted(set(entity_ids)))
    entries = tuple(sorted(set(entry_ids)))
    lines = ["---"]
    ordered: list[tuple[str, str | int | Sequence[str]]] = [
        ("weaver_schema_version", SCHEMA_VERSION),
        ("weaver_note_id", note_id),
        ("weaver_note_kind", kind),
        *properties,
        ("weaver_source_ids", sources),
        ("weaver_entity_ids", entities),
        ("weaver_entry_ids", entries),
    ]
    for key, value in ordered:
        lines.extend(_yaml(key, value))
    unsigned = "\n".join([*lines, "---", *body]).rstrip("\n") + "\n"
    digest = hashlib.sha256(unsigned.encode("utf-8")).hexdigest()
    signed = "\n".join([*lines, f"{HASH_KEY}: {digest}", "---", *body])
    data = (signed.rstrip("\n") + "\n").encode("utf-8")
    return Note(note_id, kind, data, sources, entities, entries, digest)


def embedded_hash(data: bytes) -> str | None:
    """The recorded hash if the file is an unmodified Weaver output, else None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end < 0:
        return None
    matches = list(_HASH_LINE.finditer(text, 4, end + 1))
    if len(matches) != 1:
        return None
    match = matches[0]
    unsigned = text[: match.start()] + text[match.end() :]
    digest = hashlib.sha256(unsigned.encode("utf-8")).hexdigest()
    return digest if digest == match.group(1) else None


def _relation_lines(
    entity_id: str,
    relations: Sequence[RelationLink],
    names: Mapping[str, IndexedEntity],
) -> list[str]:
    outgoing, incoming = [], []
    for rel in relations:
        reason = f" — {plain(rel.reason)}" if rel.reason else ""
        if entity_id in rel.from_ids:
            # Dataview reads `Key:: Value` to the end of the line.
            outgoing.append(f"{rel.type}:: {links(rel.to_ids, names)}{reason}")
        if entity_id in rel.to_ids:
            incoming.append(f"- {links(rel.from_ids, names)} → {rel.type}{reason}")
    lines: list[str] = []
    if outgoing:
        lines += ["", "## 관계", "", *sorted(set(outgoing))]
    if incoming:
        lines += ["", "## 들어오는 관계", "", *sorted(set(incoming))]
    return lines


def render_entity(
    entity_id: str,
    state: EntityState,
    entries: Sequence[Entry],
    relations: Sequence[RelationLink],
    names: Mapping[str, IndexedEntity],
    zone: ZoneInfo,
) -> Note:
    """Object note; a project entity's note is also its MOC."""
    known = names.get(entity_id)
    title = entity_name(entity_id, names)
    entity_type = known.type if known else "unknown"
    aliases = [one_line(known.name), *map(one_line, known.aliases)] if known else []
    retracted, superseded = statuses(entries)
    body = ["", f"# {title}", "", NOTICE]

    current: list[str] = []
    for key in sorted(state.fields):
        held = state.fields[key]
        current.append(
            f"- {plain(key)}: {value_text(held.value)} ({link(held.entry_id, names)})"
        )
    for key in sorted(state.sets):
        members = state.sets[key].members
        shown = ", ".join(
            link(m.value, names)
            if isinstance(m.value, str) and m.value.startswith("ent_")
            else value_text(m.value)
            for m in members
        )
        current.append(
            f"- {plain(key)}: {shown or '없음'} ({link(state.sets[key].entry_id, names)})"
        )
    if current:
        body += ["", "## 현재 상태", "", *current]
    if state.open_requests:
        body += ["", "## 열린 요청", ""]
        for req in state.open_requests:
            assert req.request is not None
            due = (
                f", 기한 {local_date(req.request.due_at, zone)}"
                if req.request.due_at
                else ""
            )
            body.append(
                f"- {link(req.entry_id, names)} {summary(req)} "
                f"(요청자 {link(req.request.requester, names)}, "
                f"대상 {links(req.request.targets, names)}{due})"
            )
    if state.decisions:
        body += ["", "## 결정", ""]
        for request_id in sorted(state.decisions):
            decision = state.decisions[request_id]
            body.append(
                f"- {link(request_id, names)} → {link(decision.entry_id, names)} "
                f"{summary(decision)}"
            )
    body += _relation_lines(entity_id, relations, names)

    related = sorted(
        {e for item in entries for e in item.entities if e != entity_id},
        key=lambda e: (entity_name(e, names), e),
    )
    if entity_type == "project":
        body += ["", "## 연결 대상", ""]
        body += [f"- {link(e, names)}" for e in related] or ["- 없음"]
        body += [
            "",
            "## 연결 노트",
            "",
            "```dataview",
            "TABLE weaver_entry_kind AS 종류, weaver_occurred_at AS 날짜",
            'FROM ""',
            f'WHERE weaver_note_kind = "entry" AND contains(weaver_entity_ids, "{entity_id}")',
            "SORT weaver_occurred_at ASC",
            "```",
        ]
    body += ["", "## 기록", ""]
    for item in entries:
        status = entry_status(item, retracted, superseded)
        mark = "" if status == "유효" else f" ({status})"
        body.append(
            f"- {local_date(item.occurred_at, zone)} {item.kind} "
            f"{link(item.entry_id, names)} {summary(item)}{mark}"
        )
    if not entries:
        body.append("- 없음")
    return _finish(
        entity_id,
        "entity",
        [("weaver_entity_type", entity_type), ("aliases", aliases)],
        body,
        source_ids=[item.source_id for item in entries],
        entity_ids=[entity_id],
        entry_ids=[item.entry_id for item in entries],
    )


def render_entry(
    item: Entry,
    status: str,
    names: Mapping[str, IndexedEntity],
    zone: ZoneInfo,
) -> Note:
    """Record note linking its entities, source ID and the human daily note."""
    day = local_date(item.occurred_at, zone)
    body = [
        "",
        f"# {item.kind} · {day}",
        "",
        NOTICE,
        "",
        f"- 내용: {summary(item)}",
        f"- 대상: {links(item.entities, names)}",
        f"- 데일리:: [[{day}]]",
        f"- 원본:: {item.source_id}",
        f"- 기록자: {'사람' if item.actor == 'human' else 'AI 승인'}",
        f"- 상태: {status}",
    ]
    if item.field is not None:
        body.append(f"- 필드: {plain(item.field)}")
    if item.request is not None:
        req = item.request
        body.append(f"- 요청자: {link(req.requester, names)}")
        body.append(f"- 요청 대상: {links(req.targets, names)}")
        if req.due_at is not None:
            body.append(f"- 기한: {local_date(req.due_at, zone)}")
    for label, target in (
        ("대체", item.supersedes),
        ("철회", item.retracts),
        ("해결", item.resolves),
        ("차단요청", item.blocked_by_request),
    ):
        if target is not None:
            body.append(f"- {label}:: {link(target, names)}")
    return _finish(
        item.entry_id,
        "entry",
        [
            ("weaver_entry_kind", item.kind),
            ("weaver_occurred_at", item.occurred_at.astimezone(zone).isoformat()),
            ("weaver_entry_status", status),
        ],
        body,
        source_ids=[item.source_id],
        entity_ids=item.entities,
        entry_ids=[item.entry_id],
    )
