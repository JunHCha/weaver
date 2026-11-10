"""Compile Fabric snapshots into protected wiki pages and ingest human edits."""

import fcntl
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, JsonValue

from weaver.context import ContextDictionary, Entry, Kind, Page
from weaver.fabric.v2.state import project
from weaver.fabric.v2.store import Fabric, ThreadQuery
from weaver.llm.types import LlmClient
from weaver.schema.v2 import TagCorrection, ThreadDraft
from weaver.wiki.store import HumanEdit, HumanEditPending, WikiStore


@dataclass(frozen=True)
class Summary:
    """A source's approved summary and human-readable filename stem."""

    title: str
    body: str


class EditChange(BaseModel):
    """One meaningful change identified in a human diff."""

    content: str
    tags: list[str]
    correction: TagCorrection | None = None


class EditResult(BaseModel):
    """Semantic changes; an empty list means formatting or typo only."""

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
            )
            if not db.execute("SELECT 1 FROM bookmark").fetchone():
                db.execute("INSERT INTO bookmark VALUES (0)")

    def bookmark(self) -> int:
        """Return the last fully applied sequence."""
        with sqlite3.connect(self.state_path) as db:
            return int(db.execute("SELECT seq FROM bookmark").fetchone()[0])

    def _edit(self, edit: HumanEdit) -> None:
        with sqlite3.connect(self.state_path) as db:
            row = db.execute(
                "SELECT drafts FROM edits WHERE rev=?", (edit.new_rev,)
            ).fetchone()
            if row is None:
                result = self.llm.generate_json(
                    "wiki.human_edit",
                    "Extract meaningful body changes and tag corrections; ignore typos, "
                    "formatting, and frontmatter field changes. A correction target must "
                    "be an existing thread ID from the supplied log.",
                    json.dumps(
                        {
                            "diff": edit.diff,
                            "threads": [
                                {
                                    "id": t.thread_id,
                                    "content": t.content,
                                    "tags": t.tags,
                                }
                                for t in self.fabric.entries(ThreadQuery())
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
                ) -> None:
                    drafts.append(
                        ThreadDraft(
                            thread_id=f"wiki:{edit.new_rev}:{len(drafts)}",
                            kind="태그정정" if correction else "기록",
                            content=content,
                            occurred_at=now,
                            recorded_at=now,
                            tags=tags,
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

                tag = Path(edit.path).stem
                for change in result.changes:
                    make(change.content, change.tags, correction=change.correction)
                for field in ("deadline", "schedule", "assignees", "status"):
                    before, after = (
                        edit.frontmatter_before.get(field),
                        edit.frontmatter_after.get(field),
                    )
                    if before != after:
                        if field == "assignees":
                            old = before if isinstance(before, list) else []
                            new = after if isinstance(after, list) else []
                            after = {
                                "add": [x for x in new if x not in old],
                                "remove": [x for x in old if x not in new],
                            }
                        make(f"{field} 변경", [tag], field, after)
                payload = json.dumps(
                    [d.model_dump(mode="json") for d in drafts], ensure_ascii=False
                )
                db.execute("INSERT INTO edits VALUES (?, ?)", (edit.new_rev, payload))
            else:
                payload = row[0]
        self.fabric.append([ThreadDraft.model_validate(d) for d in json.loads(payload)])
        self.store.acknowledge(edit)

    def run(self) -> None:
        """Collect settled edits and advance only after every page is durable."""
        with self.state_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._run()

    def _run(self) -> None:
        self.store.restore_readonly()
        for edit in self.store.scan(datetime.now(UTC).timestamp()):
            self._edit(edit)
        new = self.fabric.entries(ThreadQuery(after_seq=self.bookmark()))
        if not new:
            self._sync_pages()
            return
        all_threads = self.fabric.entries(ThreadQuery())
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
            entry = entries[0] if entries else Entry(tag, "topic")
            path = f"pages/{tag}.md"
            try:
                props, body, base = self.store.read(path)
                if not self.store.is_tracked_version(path, base):
                    raise HumanEditPending(path)
            except FileNotFoundError:
                props, body, base = {}, "", None
            retracted = {t.retracts for t in all_threads if t.retracts}
            related = [
                t
                for t in all_threads
                if tag in t.tags
                and t.kind not in ("태그정정", "거절")
                and t.thread_id not in retracted
                and t.retracts is None
            ]
            links = []
            for t in related:
                if t.actor == "human":
                    links.append(f"- {t.content}")
                    continue
                summary = self.summary(t.source_id)
                safe_title = summary.title.replace("/", "-").replace("\\", "-")
                summary_path = f"summaries/{safe_title} {t.source_id}.md"
                try:
                    self.store.read(summary_path)
                except FileNotFoundError:
                    self.store.write(
                        summary_path, {}, summary.body, None, readonly=True
                    )
                links.append(f"- {t.content} [[{summary_path[:-3]}]]")
            generated = self.llm.generate_text(
                "wiki.compile",
                "Update the page using the evidence; preserve human edits.",
                json.dumps({"page": body, "evidence": links}, ensure_ascii=False),
            )
            state = project(tag, all_threads)
            for field in ("deadline", "schedule", "status"):
                props[field] = (
                    state.fields[field].value if field in state.fields else None
                )
            props["assignees"] = list[JsonValue](sorted(state.assignees))
            props["kind"] = entry.kind
            props["aliases"] = list(entry.aliases)
            self.store.write(path, props, generated + "\n\n" + "\n".join(links), base)
        self._sync_pages()
        with sqlite3.connect(self.state_path) as db:
            db.execute("UPDATE bookmark SET seq=?", (max(t.seq for t in new),))

    def _sync_pages(self) -> None:
        pages = []
        directory = self.store.root / "pages"
        if directory.exists():
            for page_path in directory.glob("*.md"):
                props, body, _ = self.store.read(f"pages/{page_path.name}")
                kind = props.get("kind")
                aliases = props.get("aliases")
                if kind in ("person", "organization", "project", "topic"):
                    pages.append(
                        Page(
                            page_path.stem,
                            cast(Kind, kind),
                            tuple(x for x in aliases if isinstance(x, str))
                            if isinstance(aliases, list)
                            else (),
                            body.split("\n")[0],
                        )
                    )
        self.context.sync_from_pages(pages)
