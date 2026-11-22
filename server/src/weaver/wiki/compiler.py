"""Compile Fabric snapshots into protected wiki pages and ingest human edits."""

import fcntl
import hashlib
import json
import logging
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, JsonValue

from weaver.context import (
    ContextDictionary,
    ContextEntry,
    DictionarySnapshot,
    Kind,
    Page,
)
from weaver.fabric.state import project
from weaver.fabric.store import Fabric, ThreadQuery
from weaver.llm.errors import InvalidJson, MissingOutput, Refusal, SchemaMismatch
from weaver.llm.policy import provenance_scope
from weaver.llm.types import LlmClient
from weaver.loom.tagging import (
    MAX_NEW_ENTRIES,
    NewEntry,
    business_tags,
    canonical_tags,
    counterpart_name,
    proposals,
    tag_context,
    tag_snapshot,
)
from weaver.schema import TagCorrection, Thread, ThreadDraft
from weaver.wiki.document import (
    HUMAN_KEYS,
    MEMO,
    Hunk,
    heading_moves,
    hunks,
    restore_memo_heading,
    sections,
    text_hash,
    with_memo,
    without_memo,
)
from weaver.wiki.generation import GenerationCache, fingerprint
from weaver.wiki.links import summary_path
from weaver.wiki.prompts import HUMAN_EDIT_PROMPT
from weaver.wiki.provenance import page_source_ids
from weaver.wiki.provenance import source_ids as wiki_source_ids
from weaver.wiki.selection import (
    EvidenceIndex,
    SectionAssignments,
    assignment_map,
    section_map,
)
from weaver.wiki.store import HumanEdit, HumanEditPending, WikiStore, normalize_path

logger = logging.getLogger(__name__)
FIELD_THREAD_PREFIX = "wiki-field:"
MAX_EDIT_CONTRACT_FAILURES = 3


def _direction_particle(text: str) -> str:
    last = text.rstrip()[-1:]
    if last and "가" <= last <= "힣":
        final = (ord(last) - ord("가")) % 28
        suffix = "로" if final in (0, 8) else "으로"
    else:
        suffix = "로" if last and last in "1245789" else "으로"
    return text + suffix


def _field_text(field: str, before: JsonValue, after: JsonValue) -> str:
    def date(value: JsonValue) -> str:
        if value is None:
            return "미정"
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                return f"{parsed.month}/{parsed.day}" + (
                    f" {parsed:%H:%M}" if "T" in value else ""
                )
            except ValueError:
                return value
        return str(value)

    if field == "assignees" and isinstance(after, dict):
        add = after.get("add", [])
        remove = after.get("remove", [])
        parts = (
            [f"담당자에 {name} 추가" for name in add] if isinstance(add, list) else []
        )
        parts += (
            [f"담당자에서 {name} 제외" for name in remove]
            if isinstance(remove, list)
            else []
        )
        return ", ".join(parts) or "담당자 없음"
    if field == "deadline":
        return f"마감을 {date(before)}에서 {_direction_particle(date(after))} 변경"
    if field == "schedule":
        if isinstance(after, dict):
            start, end = after.get("start"), after.get("end")
            label = date(start)
            if isinstance(end, str):
                try:
                    label += "–" + datetime.fromisoformat(end).strftime("%H:%M")
                except ValueError:
                    label += "–" + end
            return f"일정을 {_direction_particle(label)} 변경"
        return "일정을 미정으로 변경"
    return f"상태를 {_direction_particle(str(after) if after is not None else '미정')} 변경"


def _field_tags(
    state_tag: object, dictionary: DictionarySnapshot, threads: list[Thread]
) -> list[str]:
    if not isinstance(state_tag, str) or not (entry := dictionary.resolve(state_tag)):
        logger.warning("skipped wiki fields without a resolved state tag")
        return []
    retracted = {t.retracts for t in threads if t.retracts}
    areas = {
        area.name
        for thread in threads
        if thread.thread_id not in retracted
        and thread.retracts is None
        and thread.kind not in ("태그정정", "실행", "거절")
        and any(dictionary.resolve(tag) == entry for tag in thread.tags)
        for tag in thread.tags
        if (area := dictionary.resolve(tag)) and area.kind == "area"
    }
    return list(
        dict.fromkeys([entry.name, *(sorted(areas) if len(areas) == 1 else [])])
    )


@dataclass(frozen=True)
class Summary:
    """A source's approved summary and human-readable filename stem."""

    title: str
    body: str


class EditChange(BaseModel):
    """One meaningful change identified in a human diff."""

    content: str
    tags: list[str]
    counterpart: str | None = None
    correction: TagCorrection | None = None


class HunkVerdict(BaseModel):
    id: int
    classification: Literal["fact", "structure", "expression"]
    changes: list[EditChange] = Field(default_factory=list)
    section_threads: SectionAssignments = Field(default_factory=list)


class EditResult(BaseModel):
    """Semantic changes; an empty list means formatting or typo only."""

    new_entries: list[NewEntry] = Field(
        default_factory=list, max_length=MAX_NEW_ENTRIES
    )
    changes: list[EditChange] = Field(default_factory=list)
    hunks: list[HunkVerdict] = Field(default_factory=list)


class Compiler:
    """Compile new log entries; summary lookup returns an approved source copy."""

    def __init__(
        self,
        store: WikiStore,
        fabric: Fabric,
        context: ContextDictionary,
        llm: LlmClient,
        state_path: Path,
        summary: Callable[[str], Summary],
    ) -> None:
        self.store, self.fabric, self.context, self.llm = store, fabric, context, llm
        self.state_path, self.summary = state_path, summary
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state_path) as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS bookmark (seq INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS edits (rev TEXT PRIMARY KEY, drafts TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS edit_failures (rev TEXT PRIMARY KEY, attempts INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS edit_entries (rev TEXT PRIMARY KEY, entries TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS edit_hunks (rev TEXT NOT NULL, hunk INTEGER NOT NULL, path TEXT NOT NULL, classification TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(rev, hunk));"
                "CREATE TABLE IF NOT EXISTS compiled_pages "
                "(path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, hash TEXT NOT NULL);"
            )
            if not db.execute("SELECT 1 FROM bookmark").fetchone():
                db.execute("INSERT INTO bookmark VALUES (0)")

        self.cache = GenerationCache(state_path)
        self.on_progress: Callable[[], None] | None = None

    def bookmark(self) -> int:
        """Return the last fully applied sequence."""
        with sqlite3.connect(self.state_path) as db:
            return int(db.execute("SELECT seq FROM bookmark").fetchone()[0])

    def _edit(self, edit: HumanEdit) -> None:
        """Retry contract violations per revision, then durably acknowledge no facts."""
        try:
            prepared = self._prepare_edit(edit)
            if prepared is not None:
                self._apply_edit(prepared)
        except (InvalidJson, SchemaMismatch, MissingOutput, Refusal):
            with sqlite3.connect(self.state_path) as db:
                db.execute(
                    "INSERT INTO edit_failures VALUES (?, 1) ON CONFLICT(rev) DO UPDATE SET attempts=attempts+1",
                    (edit.new_rev,),
                )
                attempts = int(
                    db.execute(
                        "SELECT attempts FROM edit_failures WHERE rev=?",
                        (edit.new_rev,),
                    ).fetchone()[0]
                )
                if attempts >= MAX_EDIT_CONTRACT_FAILURES:
                    db.execute(
                        "INSERT OR IGNORE INTO edits VALUES (?, '[]')", (edit.new_rev,)
                    )
            if attempts >= MAX_EDIT_CONTRACT_FAILURES:
                logger.warning(
                    "wiki edit contract retries exhausted; acknowledged without threads"
                )
                self._apply_edit(edit)
            else:
                logger.warning("wiki edit contract violation; retry pending")

    def _scope_tag(self, edit: HumanEdit) -> str | None:
        meta = self.store.metadata(edit.path)
        dictionary = self.context.snapshot()
        value = meta.get("state_tag") or edit.frontmatter_before.get("state_tag")
        if isinstance(value, str) and dictionary.resolve(value):
            return value
        ids = meta.get("thread_ids", [])
        areas = {
            entry.name
            for t in self.fabric.entries(ThreadQuery())
            if isinstance(ids, list) and t.thread_id in ids
            for tag in t.tags
            if (entry := dictionary.resolve(tag)) and entry.kind == "area"
        }
        return next(iter(areas)) if len(areas) == 1 else None

    def _prepare_edit(self, edit: HumanEdit) -> HumanEdit | None:
        if edit.body_before is None or edit.body_after is None:
            return edit
        edit = replace(
            edit, body_after=restore_memo_heading(edit.body_before, edit.body_after)
        )
        assert edit.body_before is not None and edit.body_after is not None
        changes = hunks(edit.body_before, edit.body_after)
        for key in sorted(set(edit.frontmatter_before) | set(edit.frontmatter_after)):
            if edit.frontmatter_before.get(key) != edit.frontmatter_after.get(key):
                changes.append(
                    Hunk(
                        len(changes),
                        "frontmatter:" + key,
                        json.dumps(
                            edit.frontmatter_before.get(key), ensure_ascii=False
                        ),
                        json.dumps(edit.frontmatter_after.get(key), ensure_ascii=False),
                        "frontmatter" if key in HUMAN_KEYS else "metadata",
                    )
                )
        old, new = (
            {s.key: s.text for s in sections(edit.body_before)},
            {s.key: s.text for s in sections(edit.body_after)},
        )
        # Renaming a heading changes identity, not business facts.
        moved = heading_moves(edit.body_before, edit.body_after)
        meta = self.store.metadata(edit.path)
        mapping = meta.get("sections", {})
        spans = meta.get("human_spans", [])
        if moved and isinstance(mapping, dict):
            for left, right in moved.items():
                if left in mapping:
                    mapping[right] = mapping.pop(left)
                if isinstance(spans, list):
                    for span in spans:
                        if isinstance(span, dict) and span.get("section") == left:
                            span["section"] = right
            self.store.write_metadata(
                edit.path, {"sections": mapping, "human_spans": spans}
            )
        code_body = ""
        for key in new:
            changed = [h for h in changes if h.section == key]
            text = new[key]
            if any(h.classification == "content" for h in changed):
                previous_key = next(
                    (left for left, right in moved.items() if right == key), key
                )
                text = old.get(previous_key, "")
                for h in changed:
                    if h.classification != "content" and h.before:
                        text = text.replace(h.before, h.after, 1)
            code_body += text
        tag = self._scope_tag(edit)
        field_changed = any(
            edit.frontmatter_before.get(k) != edit.frontmatter_after.get(k)
            for k in ("deadline", "schedule", "status", "assignees")
        )
        code_after = dict(edit.frontmatter_after)
        if field_changed and not tag:
            for key in ("deadline", "schedule", "status", "assignees"):
                if key in edit.frontmatter_before:
                    code_after[key] = edit.frontmatter_before[key]
                else:
                    code_after.pop(key, None)
        code = replace(
            edit,
            new_rev=edit.new_rev + ":code",
            body_after=code_body,
            frontmatter_after=code_after,
        )
        self._apply_edit(code)
        # Code decisions have a transaction independent of subsequent LLM work.
        with sqlite3.connect(self.state_path) as db:
            code_row = db.execute(
                "SELECT drafts FROM edits WHERE rev=?", (code.new_rev,)
            ).fetchone()
            code_drafts = json.loads(code_row[0]) if code_row else []
            db.execute("DELETE FROM edit_hunks WHERE rev=?", (code.new_rev,))
            for h in changes:
                if h.classification != "content":
                    db.execute(
                        "INSERT OR IGNORE INTO edit_hunks VALUES (?, ?, ?, ?, ?)",
                        (
                            edit.new_rev,
                            h.id,
                            edit.path,
                            h.classification,
                            json.dumps(
                                h.payload()
                                | {
                                    "thread_ids": [
                                        d["thread_id"]
                                        for d in code_drafts
                                        if h.section
                                        == "frontmatter:" + str(d.get("field"))
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
        if field_changed and not tag:
            return None
        return replace(edit, frontmatter_before=edit.frontmatter_after)

    def _apply_edit(self, edit: HumanEdit) -> None:
        with sqlite3.connect(self.state_path) as db:
            row = db.execute(
                "SELECT drafts FROM edits WHERE rev=?", (edit.new_rev,)
            ).fetchone()
            if row is None:
                field_dictionary = self.context.snapshot()
                dictionary = tag_snapshot(field_dictionary)
                threads = self.fabric.entries(ThreadQuery())
                changes = (
                    hunks(edit.body_before, edit.body_after)
                    if edit.body_before is not None and edit.body_after is not None
                    else [Hunk(0, "__legacy__", "", edit.diff, "content")]
                )
                for key in sorted(
                    set(edit.frontmatter_before) | set(edit.frontmatter_after)
                ):
                    if edit.frontmatter_before.get(key) != edit.frontmatter_after.get(
                        key
                    ):
                        changes.append(
                            Hunk(
                                len(changes),
                                "frontmatter:" + key,
                                json.dumps(
                                    edit.frontmatter_before.get(key), ensure_ascii=False
                                ),
                                json.dumps(
                                    edit.frontmatter_after.get(key), ensure_ascii=False
                                ),
                                "frontmatter" if key in HUMAN_KEYS else "metadata",
                            )
                        )
                content = [h for h in changes if h.classification == "content"]
                result = EditResult()
                meta = self.store.metadata(edit.path)
                if content:
                    index = EvidenceIndex(self.state_path, threads, field_dictionary)
                    known_ids = meta.get("thread_ids", [])
                    ids = (
                        set(str(key) for key in known_ids)
                        if isinstance(known_ids, list)
                        else set()
                    )
                    state_tag = self._scope_tag(edit)
                    ids.update(
                        index.related(
                            [state_tag] if isinstance(state_tag, str) else [],
                            [],
                            " ".join(h.after for h in content),
                        )
                    )
                    related = sorted(
                        (t for t in threads if t.thread_id in ids),
                        key=lambda t: t.seq,
                        reverse=True,
                    )[:64]
                    result = self.llm.generate_json(
                        "wiki.human_edit",
                        HUMAN_EDIT_PROMPT,
                        json.dumps(
                            {
                                **tag_context(
                                    field_dictionary,
                                    " ".join(h.after for h in content),
                                    related,
                                ),
                                "hunks": [h.payload() for h in content],
                                "threads": [
                                    {
                                        "id": t.thread_id,
                                        "content": t.content,
                                        "tags": t.tags,
                                    }
                                    for t in related
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        EditResult,
                    )
                if edit.body_before is not None and result.changes:
                    raise SchemaMismatch()
                verdicts = {v.id: v for v in result.hunks}
                if (result.hunks or content and edit.body_before is not None) and (
                    len(verdicts) != len(result.hunks)
                    or set(verdicts) != {h.id for h in content}
                ):
                    raise SchemaMismatch()
                meaningful = list(result.changes)
                owners = {
                    id(change): verdict.id
                    for verdict in result.hunks
                    if verdict.classification == "fact"
                    for change in verdict.changes
                }
                produced_by_hunk: dict[int, list[str]] = {
                    verdict.id: []
                    for verdict in result.hunks
                    if verdict.classification == "fact"
                }
                for h in changes:
                    verdict = verdicts.get(h.id)
                    kind = (
                        verdict.classification
                        if verdict
                        else (
                            "fact"
                            if h.classification == "content" and result.changes
                            else "expression"
                            if h.classification == "content"
                            else h.classification
                        )
                    )
                    if verdict and kind == "fact":
                        meaningful.extend(verdict.changes)
                    if verdict and kind == "structure" and edit.body_after is not None:
                        try:
                            assigned = assignment_map(verdict.section_threads)
                        except ValueError:
                            raise SchemaMismatch() from None
                        if any(
                            not set(ids).issubset({t.thread_id for t in threads})
                            for ids in assigned.values()
                        ):
                            raise SchemaMismatch()
                        page_ids = meta.get("thread_ids", [])
                        page_threads = [
                            t
                            for t in threads
                            if isinstance(page_ids, list)
                            and t.thread_id in page_ids
                            or t.thread_id
                            in {key for values in assigned.values() for key in values}
                        ]
                        prior_mapping = meta.get("sections", {})
                        prior_assignments: dict[str, list[str]] = {}
                        if isinstance(prior_mapping, dict):
                            for key, data in prior_mapping.items():
                                if isinstance(data, dict):
                                    assigned_ids = data.get("thread_ids")
                                    if isinstance(assigned_ids, list):
                                        prior_assignments[key] = [
                                            str(value) for value in assigned_ids
                                        ]
                        mapping = section_map(
                            edit.body_after, page_threads, prior_assignments | assigned
                        )
                        self.store.write_metadata(
                            edit.path, {"sections": json.loads(json.dumps(mapping))}
                        )
                    db.execute(
                        "INSERT OR IGNORE INTO edit_hunks VALUES (?, ?, ?, ?, ?)",
                        (
                            edit.new_rev,
                            h.id,
                            edit.path,
                            kind,
                            json.dumps(h.payload(), ensure_ascii=False),
                        ),
                    )
                if edit.body_after is not None:
                    protected = meta.get("human_spans", [])
                    spans = list(protected) if isinstance(protected, list) else []
                    for h in content + [
                        h for h in changes if h.classification == "expression"
                    ]:
                        spans = [
                            span
                            for span in spans
                            if not isinstance(span, dict)
                            or span.get("section") != h.section
                            or not isinstance(span.get("text"), str)
                            or str(span["text"]) not in h.before
                        ]
                        if h.after.strip() and (
                            h.id not in verdicts
                            or verdicts[h.id].classification != "structure"
                        ):
                            human_span: JsonValue = {
                                "section": h.section,
                                "text": h.after,
                            }
                            if human_span not in spans:
                                spans.append(human_span)
                    if spans:
                        self.store.write_metadata(edit.path, {"human_spans": spans})
                result.changes = meaningful
                revision = edit.new_rev.removesuffix(":code")
                now = datetime.now(UTC)
                # Stable ID and cached drafts ensure append retries are byte-identical.
                alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
                number = int(hashlib.sha256(revision.encode()).hexdigest()[:32], 16)
                ulid = "".join(
                    alphabet[(number >> (5 * i)) & 31] for i in reversed(range(26))
                )
                source_id = "src_" + ulid
                drafts: list[ThreadDraft] = []

                def make(
                    content: str,
                    tags: list[str],
                    field: Literal["deadline", "schedule", "assignees", "status"]
                    | None = None,
                    value: JsonValue = None,
                    correction: TagCorrection | None = None,
                    counterpart: str | None = None,
                ) -> None:
                    drafts.append(
                        ThreadDraft(
                            thread_id=f"{FIELD_THREAD_PREFIX if field else 'wiki:'}{revision}:{len(drafts)}",
                            kind="태그정정" if correction else "기록",
                            content=content,
                            occurred_at=now,
                            recorded_at=now,
                            tags=tags,
                            counterpart=counterpart,
                            field=field,
                            value=value,
                            source_id=source_id,
                            actor="human",
                            direction=None,
                            resolves=None,
                            retracts=None,
                            correction=correction,
                        )
                    )

                state_tag = self._scope_tag(edit)
                additions, names = proposals(field_dictionary, result.new_entries)
                for change in result.changes:
                    tags = (
                        canonical_tags(dictionary, names, change.tags)
                        if change.correction
                        else business_tags(dictionary, additions, names, change.tags)
                    )
                    correction = change.correction
                    if correction:
                        if correction.target not in {t.thread_id for t in threads}:
                            continue
                        correction = correction.model_copy(
                            update={
                                "add": canonical_tags(dictionary, names, correction.add)
                            }
                        )
                    counterpart = (
                        counterpart_name(
                            dictionary, additions, names, change.counterpart
                        )
                        if not correction
                        else None
                    )
                    if counterpart is not None and counterpart not in tags:
                        tags.append(counterpart)
                    make(
                        change.content,
                        tags,
                        correction=correction,
                        counterpart=counterpart,
                    )
                    owner = owners.get(id(change))
                    if owner is not None:
                        produced_by_hunk[owner].append(drafts[-1].thread_id)
                used = (
                    {tag for d in drafts for tag in d.tags}
                    | {d.counterpart for d in drafts if d.counterpart}
                    | {tag for d in drafts if d.correction for tag in d.correction.add}
                )
                db.execute(
                    "INSERT INTO edit_entries VALUES (?, ?)",
                    (
                        edit.new_rev,
                        json.dumps(
                            [e.model_dump() for e in additions if e.name in used],
                            ensure_ascii=False,
                        ),
                    ),
                )
                for field in ("deadline", "schedule", "assignees", "status"):
                    before, after = (
                        edit.frontmatter_before.get(field),
                        edit.frontmatter_after.get(field),
                    )
                    if before != after:
                        field_tags = _field_tags(state_tag, field_dictionary, threads)
                        if not field_tags:
                            continue
                        if field == "assignees":
                            old = before if isinstance(before, list) else []
                            new = after if isinstance(after, list) else []
                            after = {
                                "add": [x for x in new if x not in old],
                                "remove": [x for x in old if x not in new],
                            }
                        make(
                            _field_text(field, before, after), field_tags, field, after
                        )
                for hunk_id, identifiers in produced_by_hunk.items():
                    raw_hunk = db.execute(
                        "SELECT payload FROM edit_hunks WHERE rev=? AND hunk=?",
                        (edit.new_rev, hunk_id),
                    ).fetchone()
                    if raw_hunk:
                        detail = json.loads(raw_hunk[0])
                        detail["draft_ids"] = identifiers
                        db.execute(
                            "UPDATE edit_hunks SET payload=? WHERE rev=? AND hunk=?",
                            (
                                json.dumps(detail, ensure_ascii=False),
                                edit.new_rev,
                                hunk_id,
                            ),
                        )
                payload = json.dumps(
                    [d.model_dump(mode="json") for d in drafts], ensure_ascii=False
                )
                db.execute("INSERT INTO edits VALUES (?, ?)", (edit.new_rev, payload))
            else:
                payload = row[0]
            entry_row = db.execute(
                "SELECT entries FROM edit_entries WHERE rev=?", (edit.new_rev,)
            ).fetchone()
        for raw in json.loads(entry_row[0]) if entry_row else []:
            entry = NewEntry.model_validate(raw)
            self.context.add_entry(
                entry.name,
                entry.kind,
                tuple(entry.aliases),
                human_edited=False,
                drop_conflicting_aliases=True,
                source_ids=tuple(
                    sorted(
                        page_source_ids(
                            self.store.metadata(edit.path),
                            {
                                t.thread_id: t.source_id
                                for t in self.fabric.entries(ThreadQuery())
                            },
                        )
                    )
                ),
            )
        appended = self.fabric.append(
            [ThreadDraft.model_validate(d) for d in json.loads(payload)]
        )
        with sqlite3.connect(self.state_path) as db:
            for hunk_id, raw in db.execute(
                "SELECT hunk, payload FROM edit_hunks WHERE rev=?", (edit.new_rev,)
            ).fetchall():
                detail = json.loads(raw)
                expected_ids = detail.pop("draft_ids", detail.get("thread_ids"))
                produced = [
                    t.thread_id
                    for t in appended
                    if (
                        t.thread_id in expected_ids
                        if isinstance(expected_ids, list)
                        else detail.get("section") == "frontmatter:" + str(t.field)
                        if t.field is not None
                        else detail.get("classification") == "content"
                    )
                ]
                detail["thread_ids"] = list(
                    dict.fromkeys([*detail.get("thread_ids", []), *produced])
                )
                db.execute(
                    "UPDATE edit_hunks SET payload=? WHERE rev=? AND hunk=?",
                    (json.dumps(detail, ensure_ascii=False), edit.new_rev, hunk_id),
                )
        if edit.body_before is not None and edit.path.startswith("pages/"):
            try:
                props, body, base = self.store.read(edit.path)
                # Only restore this acknowledged version, never a newer edit.
                if self.store.is_tracked_version(edit.path, base):
                    trusted = self.store.metadata(edit.path)
                    disk_body = body
                    body = restore_memo_heading(edit.body_before, body)
                    memo_source = body
                    before_parts = {
                        part.key: part.text for part in sections(edit.body_before)
                    }
                    if "## 근거" in before_parts:
                        body = without_memo(
                            "".join(
                                part.text
                                for part in sections(body)
                                if part.key not in ("## 근거", MEMO)
                            )
                        )
                        body = with_memo(
                            body.rstrip() + "\n\n" + before_parts["## 근거"],
                            memo_source,
                        )
                    # Memo headings may vary in spacing; an unrelated rename is
                    # restored without throwing away the person's note text.
                    if MEMO in before_parts and not any(
                        part.key == MEMO for part in sections(body)
                    ):
                        parts = sections(body)
                        if parts and parts[-1].key not in before_parts:
                            note = MEMO + "\n" + parts[-1].text.partition("\n")[2]
                            body = "".join(part.text for part in parts[:-1]) + note
                    bad = any(
                        key not in HUMAN_KEYS and key != "weaver" for key in props
                    ) or props.get("weaver") != trusted.get("id")
                    if bad or body != disk_body:
                        self.store.write(
                            edit.path,
                            trusted
                            | {
                                key: value
                                for key, value in props.items()
                                if key in HUMAN_KEYS
                            },
                            body,
                            base,
                            metadata="restore private wiki fields",
                            memo_source=memo_source,
                        )
            except FileNotFoundError:
                pass
        if edit.path.startswith("pages/"):
            try:
                _, current_body, digest = self.store.read(edit.path)
                if self.store.is_tracked_version(edit.path, digest):
                    current_mapping = self.store.metadata(edit.path).get("sections", {})
                    if isinstance(current_mapping, dict):
                        for part in sections(current_body):
                            data = current_mapping.get(part.key)
                            if isinstance(data, dict):
                                data.update(
                                    hash=text_hash(part.text),
                                    search_text=part.text[:1000],
                                )
                        self.store.write_metadata(
                            edit.path, {"hash": digest, "sections": current_mapping}
                        )
            except FileNotFoundError:
                pass
        self.store.acknowledge(edit)
        if self.on_progress is not None:
            self.on_progress()

    def run(self) -> None:
        """Collect settled edits and advance only after every page is durable."""
        with self.state_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            threads = self.fabric.entries(ThreadQuery())
            with provenance_scope(
                {t.source_id for t in threads}
                | wiki_source_ids(
                    self.store, {t.thread_id: t.source_id for t in threads}
                ),
                shared=True,
            ):
                self._run()

    def _run(self) -> None:
        self.store.restore_readonly()
        for edit in self.store.scan(self.store.clock()):
            self._edit(edit)
        new = self.fabric.entries(ThreadQuery(after_seq=self.bookmark()))
        if not new:
            self._sync_pages()
            return
        high = max(t.seq for t in new)
        all_threads = [t for t in self.fabric.entries(ThreadQuery()) if t.seq <= high]
        tags = {tag for t in new for tag in t.tags}
        for t in new:
            if t.correction:
                tags.update(t.correction.remove + t.correction.add)
            if t.retracts:
                tags.update(
                    tag
                    for target in all_threads
                    if target.thread_id == t.retracts
                    for tag in target.tags
                )
        for tag in sorted(tags):
            entries = [
                e for e in self.context.candidates(tag, limit=100) if e.name == tag
            ]
            entry = entries[0] if entries else ContextEntry(tag, "topic")
            path = normalize_path(f"pages/{tag}.md")
            try:
                props, body, base = self.store.read(path)
                if not self.store.is_tracked_version(path, base):
                    raise HumanEditPending(path)
            except FileNotFoundError:
                props, body, base = {}, "", None
            snapshot = fingerprint(
                {
                    "threads": [t.model_dump(mode="json") for t in all_threads],
                    "kind": entry.kind,
                    "aliases": entry.aliases,
                }
            )
            with sqlite3.connect(self.state_path) as db:
                completed = db.execute(
                    "SELECT 1 FROM compiled_pages WHERE path=? AND fingerprint=? AND hash=?",
                    (path, snapshot, base),
                ).fetchone()
            if completed:
                continue
            self.store.check_write(path, base)
            retracted = {t.retracts for t in all_threads if t.retracts}
            related = [
                t
                for t in all_threads
                if tag in t.tags
                and t.kind not in ("태그정정", "거절", "실행")
                and t.thread_id not in retracted
                and t.retracts is None
            ]
            links = []
            for t in related:
                if t.actor == "human":
                    links.append(f"- {t.content}")
                    continue
                summary = self.summary(t.source_id)
                source_path = summary_path(summary.title, t.source_id)
                try:
                    self.store.read(source_path)
                except FileNotFoundError:
                    self.store.write(source_path, {}, summary.body, None, readonly=True)
                links.append(f"- {t.content} [[{source_path[:-3]}]]")
            payload = json.dumps({"page": body, "evidence": links}, ensure_ascii=False)
            generated = self.cache.get(
                path,
                {"payload": payload, "base": base},
                partial(
                    self.llm.generate_text,
                    "wiki.compile",
                    "Update the page using the evidence; preserve human edits.",
                    payload,
                ),
            )
            state = project(tag, all_threads)
            for field in ("deadline", "schedule", "status"):
                props[field] = (
                    state.fields[field].value if field in state.fields else None
                )
            props["assignees"] = list[JsonValue](sorted(state.assignees))
            props["kind"] = entry.kind
            props["aliases"] = list(entry.aliases)
            written = self.store.write(
                path, props, generated + "\n\n" + "\n".join(links), base
            )
            with sqlite3.connect(self.state_path) as db:
                db.execute(
                    "INSERT OR REPLACE INTO compiled_pages VALUES (?, ?, ?)",
                    (path, snapshot, written),
                )
            self.cache.discard(path)
        self._sync_pages()
        with sqlite3.connect(self.state_path) as db:
            db.execute("UPDATE bookmark SET seq=?", (max(t.seq for t in new),))

    def _sync_pages(self) -> None:
        pages = []
        directory = self.store.root / "pages"
        if directory.exists():
            for page_path in directory.glob("*.md"):
                props, body, _ = self.store.read(f"pages/{page_path.name}")
                props = self.store.metadata(f"pages/{page_path.name}") | props
                role = props.get("page_role")
                kind = {
                    "operation": "area",
                    "reference": "topic",
                    "project": "project",
                    "person": "person",
                    "organization": "organization",
                }.get(str(role), props.get("kind"))
                aliases = props.get("aliases")
                if kind in ("person", "organization", "project", "area", "topic"):
                    pages.append(
                        Page(
                            unicodedata.normalize("NFC", page_path.stem),
                            cast(Kind, kind),
                            tuple(x for x in aliases if isinstance(x, str))
                            if isinstance(aliases, list)
                            else (),
                            str(props.get("summary") or body.split("\n")[0]),
                            tuple(
                                sorted(
                                    page_source_ids(
                                        self.store.metadata(f"pages/{page_path.name}"),
                                        {
                                            t.thread_id: t.source_id
                                            for t in self.fabric.entries(ThreadQuery())
                                        },
                                    )
                                )
                            ),
                        )
                    )
        self.context.sync_from_pages(pages)
