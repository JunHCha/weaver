"""A person's feedback on a document: stored, classified once, applied, undoable.

A factual correction becomes human Threads through the same extraction a Wiki
edit uses. A structure instruction becomes a directive that the next document
compile receives. Undo retracts the created Threads and switches the directive
off. State lives in its own SQLite file; the LLM only classifies.
"""

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict

from weaver.context.proposals import ProposalDraft
from weaver.fabric import ThreadQuery
from weaver.fabric.store import Fabric
from weaver.llm.errors import (
    InvalidJson,
    LlmError,
    MissingOutput,
    Refusal,
    SchemaMismatch,
)
from weaver.llm.policy import provenance_scope
from weaver.llm.types import LlmClient
from weaver.loom.tagging import MAX_NEW_ENTRIES, NewEntry, tag_context
from weaver.schema import Thread, ThreadDraft
from weaver.schema.models import FeedbackChip, FeedbackItem
from weaver.wiki.compiler import EditChange, revision_source_id
from weaver.wiki.documents import DocumentCompiler
from weaver.wiki.prompts import FEEDBACK_PROMPT
from weaver.wiki.provenance import page_source_ids

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
MAX_THREADS = 64
MAX_DIRECTIVES = 5
MAX_DIRECTIVE = 300
CHIPS: tuple[str, ...] = get_args(FeedbackChip.__value__)
_CONTENT_FAILURES = (SchemaMismatch, InvalidJson, MissingOutput, Refusal)
UNDO_TEXT = "피드백 반영을 되돌림"


class FeedbackNotFound(LookupError):
    """No feedback has this ID."""


class FeedbackAlreadyUndone(Exception):
    """The feedback was undone before."""


# LLM output: no length or count limits; the processor clips per item.
class FeedbackVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: Literal["fact", "structure", "both", "none"]
    changes: list[EditChange] = []
    new_entries: list[NewEntry] = []
    directive: str | None = None


def feedback_id(client_key: str) -> str:
    return "fbk_" + hashlib.sha256(client_key.encode()).hexdigest()[:32]


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


_COLUMNS = (
    "feedback_id, document_id, bullet_id, text, chips, status, classification, "
    "result, thread_ids, created_at, updated_at"
)


class FeedbackStore:
    """SQLite store of feedback items, their stored plans and active directives."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS feedback ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "feedback_id TEXT NOT NULL UNIQUE, "
                "client_key TEXT NOT NULL UNIQUE, "
                "document_id TEXT NOT NULL, page_key TEXT NOT NULL, "
                "bullet_id TEXT, text TEXT NOT NULL, chips TEXT NOT NULL, "
                "status TEXT NOT NULL, classification TEXT, result TEXT, "
                "thread_ids TEXT NOT NULL, directive TEXT, "
                "directive_active INTEGER NOT NULL DEFAULT 0, "
                "plan TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
                "retract_pending INTEGER NOT NULL DEFAULT 0, undone_at TEXT, "
                "undone_key TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS changed (page_key TEXT PRIMARY KEY)")

    def _secure(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm", "-journal"):
            with suppress(FileNotFoundError):
                os.chmod(f"{self.path}{suffix}", 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._secure()
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            yield db
        self._secure()

    @staticmethod
    def _item(row: tuple[Any, ...]) -> FeedbackItem:
        return FeedbackItem.model_validate(
            {
                "feedback_id": row[0],
                "document_id": row[1],
                "bullet_id": row[2],
                "text": row[3],
                "chips": json.loads(row[4]),
                "status": row[5],
                "classification": row[6],
                "result": row[7],
                "thread_ids": json.loads(row[8]),
                "created_at": row[9],
                "updated_at": row[10],
            }
        )

    # -- writes -------------------------------------------------------------

    def submit(
        self,
        *,
        document_id: str,
        page_key: str,
        bullet_id: str | None,
        text: str,
        chips: list[str],
        client_key: str,
        now: datetime,
    ) -> FeedbackItem:
        """Store a note; the same ``client_key`` always returns the same item."""
        stamp = _stamp(now)
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO feedback (feedback_id, client_key, "
                "document_id, page_key, bullet_id, text, chips, status, "
                "thread_ids, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '[]', ?, ?)",
                (
                    feedback_id(client_key),
                    client_key,
                    document_id,
                    page_key,
                    bullet_id,
                    text,
                    json.dumps(list(dict.fromkeys(chips)), ensure_ascii=False),
                    stamp,
                    stamp,
                ),
            )
        item = self.get(feedback_id(client_key))
        assert item is not None
        return item

    def mark(
        self,
        fid: str,
        *,
        classification: str,
        result: str,
        thread_ids: list[str],
        directive: str | None,
        now: datetime,
    ) -> None:
        """Finish a pending item. An item undone meanwhile keeps its status but
        its new Threads are still queued for retraction."""
        with self._connect() as db:
            row = db.execute(
                "SELECT page_key, status FROM feedback WHERE feedback_id=?", (fid,)
            ).fetchone()
            if row is None:
                raise FeedbackNotFound(fid)
            page_key, status = row
            live = status == "pending"
            db.execute(
                "UPDATE feedback SET classification=?, result=?, thread_ids=?, "
                "directive=?, directive_active=?, status=?, retract_pending=?, "
                "updated_at=? WHERE feedback_id=?",
                (
                    classification,
                    result,
                    json.dumps(thread_ids),
                    directive,
                    int(live and directive is not None),
                    "applied" if live else status,
                    int(status == "undone" and bool(thread_ids)),
                    _stamp(now),
                    fid,
                ),
            )
            if live and directive is not None:
                db.execute("INSERT OR IGNORE INTO changed VALUES (?)", (page_key,))

    def fail(self, fid: str, *, result: str, now: datetime) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE feedback SET status='failed', result=?, updated_at=? "
                "WHERE feedback_id=? AND status='pending'",
                (result, _stamp(now), fid),
            )

    def fail_appended(
        self, fid: str, *, result: str, thread_ids: list[str], now: datetime
    ) -> None:
        """Fail an item whose Threads were already appended; queue their retraction."""
        with self._connect() as db:
            db.execute(
                "UPDATE feedback SET status=CASE WHEN status='pending' "
                "THEN 'failed' ELSE status END, result=?, thread_ids=?, "
                "undone_at=COALESCE(undone_at, ?), retract_pending=1, updated_at=? "
                "WHERE feedback_id=?",
                (result, json.dumps(thread_ids), _stamp(now), _stamp(now), fid),
            )

    def record_attempt(self, fid: str) -> int:
        with self._connect() as db:
            db.execute(
                "UPDATE feedback SET attempts=attempts+1 WHERE feedback_id=?", (fid,)
            )
            row = db.execute(
                "SELECT attempts FROM feedback WHERE feedback_id=?", (fid,)
            ).fetchone()
        return int(row[0]) if row else 0

    def save_plan(self, fid: str, plan: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE feedback SET plan=? WHERE feedback_id=?",
                (json.dumps(plan, ensure_ascii=False), fid),
            )

    def undo(self, fid: str, now: datetime, key: str | None = None) -> FeedbackItem:
        """Mark undone, switch the directive off and queue Thread retractions.

        The same ``key`` repeating an undo it already made returns the item."""
        with self._connect() as db:
            row = db.execute(
                "SELECT status, page_key, thread_ids, directive_active, undone_key "
                "FROM feedback WHERE feedback_id=?",
                (fid,),
            ).fetchone()
            if row is None:
                raise FeedbackNotFound(fid)
            status, page_key, thread_ids, active, undone_key = row
            if status == "undone":
                if key is not None and key == undone_key:
                    item = self.get(fid)
                    assert item is not None
                    return item
                raise FeedbackAlreadyUndone(fid)
            db.execute(
                "UPDATE feedback SET status='undone', directive_active=0, "
                "retract_pending=?, undone_at=?, undone_key=?, updated_at=? "
                "WHERE feedback_id=?",
                (
                    int(status == "applied" and json.loads(thread_ids) != []),
                    _stamp(now),
                    key,
                    _stamp(now),
                    fid,
                ),
            )
            if active:
                db.execute("INSERT OR IGNORE INTO changed VALUES (?)", (page_key,))
        item = self.get(fid)
        assert item is not None
        return item

    def retractions_done(self, fid: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE feedback SET retract_pending=0 WHERE feedback_id=?", (fid,)
            )

    def take_changed_pages(self) -> list[str]:
        """Pages whose directives changed; the caller marks them for recompile."""
        with self._connect() as db:
            return [r[0] for r in db.execute("SELECT page_key FROM changed")]

    def release_changed_page(self, page_key: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM changed WHERE page_key=?", (page_key,))

    # -- reads --------------------------------------------------------------

    def get(self, fid: str) -> FeedbackItem | None:
        with self._connect() as db:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM feedback WHERE feedback_id=?", (fid,)
            ).fetchone()
        return None if row is None else self._item(row)

    def pending(self) -> list[FeedbackItem]:
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM feedback WHERE status='pending' ORDER BY seq"
            ).fetchall()
        return [self._item(r) for r in rows]

    def for_document(self, document_id: str) -> list[FeedbackItem]:
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM feedback WHERE document_id=? "
                "ORDER BY seq DESC",
                (document_id,),
            ).fetchall()
        return [self._item(r) for r in rows]

    def page_key(self, fid: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT page_key FROM feedback WHERE feedback_id=?", (fid,)
            ).fetchone()
        if row is None:
            raise FeedbackNotFound(fid)
        return str(row[0])

    def bullet_id(self, fid: str) -> str | None:
        item = self.get(fid)
        return item.bullet_id if item else None

    def plan(self, fid: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT plan FROM feedback WHERE feedback_id=?", (fid,)
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def directives(self, page_key: str) -> list[str]:
        """Active structure directives for a page, oldest first, latest five."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT directive FROM feedback WHERE page_key=? AND status='applied' "
                "AND directive_active=1 AND directive IS NOT NULL "
                "AND classification IN ('structure', 'both') "
                "ORDER BY seq DESC LIMIT ?",
                (page_key, MAX_DIRECTIVES),
            ).fetchall()
        return [r[0] for r in reversed(rows)]

    def pending_retractions(self) -> list[tuple[str, list[str], datetime]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT feedback_id, thread_ids, undone_at FROM feedback "
                "WHERE retract_pending=1"
            ).fetchall()
        return [(r[0], json.loads(r[1]), datetime.fromisoformat(r[2])) for r in rows]


def finish_retractions(store: FeedbackStore, fabric: Fabric) -> int:
    """Append the retraction Threads of undone feedback; safe to repeat."""
    done = 0
    queued = store.pending_retractions()
    if not queued:
        return 0
    by_id: dict[str, Thread] = {t.thread_id: t for t in fabric.entries(ThreadQuery())}
    for fid, thread_ids, at in queued:
        source_id = revision_source_id(f"feedback:{fid}")
        drafts = [
            ThreadDraft(
                thread_id=f"feedback-undo:{fid}:{n}",
                kind="결정",
                content=UNDO_TEXT,
                occurred_at=at,
                recorded_at=at,
                tags=by_id[tid].tags,
                counterpart=by_id[tid].counterpart,
                field=None,
                value=None,
                source_id=source_id,
                actor="human",
                direction=None,
                resolves=None,
                retracts=tid,
                correction=None,
            )
            for n, tid in enumerate(thread_ids)
            if tid in by_id
        ]
        if drafts:
            fabric.append(drafts)
        store.retractions_done(fid)
        done += 1
    return done


def undo_feedback(
    store: FeedbackStore,
    fabric: Fabric,
    fid: str,
    now: datetime,
    key: str | None = None,
) -> FeedbackItem:
    """Undo a note: retract its Threads, switch its directive off. Never reprocess."""
    item = store.undo(fid, now, key)
    finish_retractions(store, fabric)
    return item


def _clean(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit].rstrip()


class FeedbackProcessor:
    """Classify pending feedback with one LLM call each and apply the result."""

    def __init__(
        self,
        compiler: DocumentCompiler,
        store: FeedbackStore,
        fabric: Fabric,
        llm: LlmClient,
    ) -> None:
        self.compiler, self.store, self.fabric, self.llm = (
            compiler,
            store,
            fabric,
            llm,
        )

    def undo(self, fid: str, now: datetime, key: str | None = None) -> FeedbackItem:
        return undo_feedback(self.store, self.fabric, fid, now, key)

    def process(self) -> int:
        """Handle every pending item once; returns how many were applied."""
        finish_retractions(self.store, self.fabric)
        applied = 0
        for item in self.store.pending():
            try:
                plan = self.store.plan(item.feedback_id)
                if plan is None:
                    plan = self._plan(item)
                    if plan is None:
                        continue
                    # Persist the plan first so a retry appends identical drafts.
                    self.store.save_plan(item.feedback_id, plan)
                self._apply(item, plan)
            except LlmError:
                # Limits and outages are the caller's to wait out.
                raise
            except Exception:
                logger.exception("feedback processing failed")
                self.store.fail(
                    item.feedback_id, result="반영하지 못함", now=datetime.now(UTC)
                )
                continue
            applied += 1
        self._release_directive_changes()
        return applied

    def _release_directive_changes(self) -> None:
        for page_key in self.store.take_changed_pages():
            self.compiler.mark_dirty(page_key)
            self.store.release_changed_page(page_key)

    def _evidence(
        self, item: FeedbackItem, page_key: str
    ) -> tuple[list[Any], list[Thread]]:
        store = self.compiler.store
        meta = store.metadata(f"pages/{page_key}.md")
        bullets = self.compiler.bullets(page_key)
        shown = [
            {"id": f"b{i}", "text": b.text, "thread_ids": b.thread_ids}
            for i, b in enumerate(bullets, 1)
        ]
        ids: list[str] = []
        target = next((b for b in shown if b["id"] == item.bullet_id), None)
        if target is not None:
            shown = [target]
            ids = list(target["thread_ids"])
        else:
            known = meta.get("thread_ids", [])
            ids = [str(k) for k in known] if isinstance(known, list) else []
        by_id = {t.thread_id: t for t in self.fabric.entries(ThreadQuery())}
        threads = sorted(
            (by_id[i] for i in dict.fromkeys(ids) if i in by_id),
            key=lambda t: t.seq,
            reverse=True,
        )[:MAX_THREADS]
        return shown, threads

    def _plan(self, item: FeedbackItem) -> dict[str, Any] | None:
        page_key = self.store.page_key(item.feedback_id)
        bullets, threads = self._evidence(item, page_key)
        snapshot = self.compiler.context.snapshot()
        payload = {
            **tag_context(snapshot, item.text, threads),
            "feedback": {
                "text": item.text,
                "chips": item.chips,
                "bullet_id": item.bullet_id,
            },
            "document": {"key": page_key},
            "bullets": bullets,
            "threads": [
                {"id": t.thread_id, "content": t.content, "tags": t.tags}
                for t in threads
            ],
        }
        everything = {
            t.thread_id: t.source_id for t in self.fabric.entries(ThreadQuery())
        }
        scope = {t.source_id for t in threads} | page_source_ids(
            self.compiler.store.metadata(f"pages/{page_key}.md"), everything
        )
        try:
            # Same ADR-046 scope as a compile: the model sees this page's sources.
            with provenance_scope(scope, shared=True):
                verdict = self.llm.generate_json(
                    "wiki.feedback",
                    FEEDBACK_PROMPT,
                    json.dumps(payload, ensure_ascii=False),
                    FeedbackVerdict,
                )
        except _CONTENT_FAILURES as error:
            attempts = self.store.record_attempt(item.feedback_id)
            logger.warning("feedback classification failed: %s", type(error).__name__)
            if attempts >= MAX_ATTEMPTS:
                self.store.fail(
                    item.feedback_id,
                    result="분류하지 못함",
                    now=datetime.now(UTC),
                )
            return None
        kind = verdict.classification
        drafts: list[ThreadDraft] = []
        locations: dict[str, ProposalDraft] = {}
        used: list[NewEntry] = []
        if kind in ("fact", "both"):
            built = self.compiler.draft_changes(
                f"feedback:{item.feedback_id}",
                verdict.changes,
                verdict.new_entries[:MAX_NEW_ENTRIES],
                threads,
            )
            drafts, locations = built.drafts, built.locations
            names = (
                {tag for d in drafts for tag in d.tags}
                | {d.counterpart for d in drafts if d.counterpart}
                | {t for d in drafts if d.correction for t in d.correction.add}
            )
            used = [e for e in built.additions if e.name in names]
        directive = (
            _clean(verdict.directive or "", MAX_DIRECTIVE)
            if kind in ("structure", "both")
            else ""
        )
        return {
            "classification": kind,
            "drafts": [d.model_dump(mode="json") for d in drafts],
            "locations": [p.model_dump() for p in locations.values()],
            "entries": [e.model_dump() for e in used],
            "directive": directive or None,
        }

    def _apply(self, item: FeedbackItem, plan: dict[str, Any]) -> None:
        drafts = [ThreadDraft.model_validate(d) for d in plan["drafts"]]
        directive: str | None = plan["directive"]
        source_ids = tuple(sorted({d.source_id for d in drafts}))
        for raw in plan["entries"]:
            entry = NewEntry.model_validate(raw)
            self.compiler.context.add_entry(
                entry.name,
                entry.kind,
                tuple(entry.aliases),
                human_edited=False,
                drop_conflicting_aliases=True,
                source_ids=source_ids,
            )
        appended = self.fabric.append(drafts) if drafts else []
        try:
            self._finish(item, plan, appended, directive)
        except Exception:
            if appended:
                # No live Threads may remain for a failed item.
                self.store.fail_appended(
                    item.feedback_id,
                    result="반영하지 못함",
                    thread_ids=[t.thread_id for t in appended],
                    now=datetime.now(UTC),
                )
                finish_retractions(self.store, self.fabric)
            raise

    def _finish(
        self,
        item: FeedbackItem,
        plan: dict[str, Any],
        appended: list[Thread],
        directive: str | None,
    ) -> None:
        proposals = self.compiler.location_proposals
        if proposals is not None:
            for raw in plan["locations"]:
                proposals.propose(
                    **ProposalDraft.model_validate(raw).model_dump(),
                    now=appended[0].recorded_at if appended else datetime.now(UTC),
                )
        now = datetime.now(UTC)
        zone = self.compiler.timezone
        parts: list[str] = []
        if appended:
            parts.append(f"정정 {len(appended)}건 반영 · {now.astimezone(zone):%H:%M}")
        if directive:
            parts.append("구성 지시 저장 · 다음 컴파일에 반영")
        self.store.mark(
            item.feedback_id,
            classification=plan["classification"],
            result=" / ".join(parts) or "반영할 내용 없음",
            thread_ids=[t.thread_id for t in appended],
            directive=directive,
            now=now,
        )
