"""Compile Fabric snapshots into protected wiki pages and ingest human edits."""

import fcntl
import hashlib
import json
import logging
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
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
from weaver.wiki.generation import GenerationCache, fingerprint
from weaver.wiki.links import summary_path
from weaver.wiki.prompts import HUMAN_EDIT_PROMPT
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


class EditResult(BaseModel):
    """Semantic changes; an empty list means formatting or typo only."""

    new_entries: list[NewEntry] = Field(
        default_factory=list, max_length=MAX_NEW_ENTRIES
    )
    changes: list[EditChange]


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
            self._apply_edit(edit)
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

    def _apply_edit(self, edit: HumanEdit) -> None:
        with sqlite3.connect(self.state_path) as db:
            row = db.execute(
                "SELECT drafts FROM edits WHERE rev=?", (edit.new_rev,)
            ).fetchone()
            if row is None:
                field_dictionary = self.context.snapshot()
                dictionary = tag_snapshot(field_dictionary)
                threads = self.fabric.entries(ThreadQuery())
                result = self.llm.generate_json(
                    "wiki.human_edit",
                    HUMAN_EDIT_PROMPT,
                    json.dumps(
                        {
                            **tag_context(field_dictionary, edit.diff, threads),
                            "diff": edit.diff,
                            "threads": [
                                {
                                    "id": t.thread_id,
                                    "content": t.content,
                                    "tags": t.tags,
                                }
                                for t in threads
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    EditResult,
                )
                now = datetime.now(UTC)
                # Stable ID and cached drafts ensure append retries are byte-identical.
                alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
                number = int(hashlib.sha256(edit.new_rev.encode()).hexdigest()[:32], 16)
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
                            thread_id=f"{FIELD_THREAD_PREFIX if field else 'wiki:'}{edit.new_rev}:{len(drafts)}",
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

                state_tag = edit.frontmatter_after.get("state_tag")
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
            )
        self.fabric.append([ThreadDraft.model_validate(d) for d in json.loads(payload)])
        self.store.acknowledge(edit)
        if self.on_progress is not None:
            self.on_progress()

    def run(self) -> None:
        """Collect settled edits and advance only after every page is durable."""
        with self.state_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
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
                        )
                    )
        self.context.sync_from_pages(pages)
