"""Compile one bundled PARA document per page key, with bullet-level evidence.

The model writes only the "current state" bullets and caution wording. Dates,
remaining work and reference links come from code, and every bullet must cite
Thread IDs from its own document's input or it is dropped.
"""

import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, JsonValue

from weaver.context import ContextDictionary, DictionarySnapshot
from weaver.context.tree import node_kind
from weaver.fabric import ThreadQuery
from weaver.fabric.store import Fabric
from weaver.llm.errors import InvalidJson, MissingOutput, Refusal, SchemaMismatch
from weaver.llm.types import LlmClient
from weaver.mobile.read import decorate
from weaver.reference import find_references
from weaver.schema import Thread
from weaver.wiki import layout
from weaver.wiki.batch import PromotionItem
from weaver.wiki.checkbox import CheckboxWatcher
from weaver.wiki.compiler import Compiler, Summary
from weaver.wiki.digest import DigestBuilder, DigestEntry
from weaver.wiki.generation import fingerprint
from weaver.wiki.promotion import PROJECT_THREAD_PREFIX, ProjectLifecycle
from weaver.wiki.prompts import DOCUMENT_PROMPT, RULES_VERSION
from weaver.wiki.selection import MAX_NEW_THREADS, MAX_RELATED_THREADS
from weaver.wiki.snapshot import canonical_threads
from weaver.wiki.store import HumanEditPending, WikiStore

logger = logging.getLogger(__name__)

MAX_BULLETS = 5
MAX_KEYS_PER_CALL = 8
MAX_ATTEMPTS = 2
_CONTENT_FAILURES = (SchemaMismatch, InvalidJson, MissingOutput, Refusal)
_OLD_ROOT_PAGES = ("index.md", "log.md")
MAX_LINE = 140
MAX_TITLE = 40
_BAD_TITLE = re.compile(r"[/\\#^|\[\]\x00-\x1f\x7f]")
_EXCLUDED = ("태그정정", "거절", "실행")


# These models describe LLM output. They carry no length or count limits: the
# provider wire schema drops them, so one violation would fail the whole batch.
# `DocumentCompiler._accept` clips or drops per item instead.
class BulletText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    thread_ids: list[str] = []


class CautionText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    text: str


class DocumentText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str = ""
    current: list[BulletText] = []
    cautions: list[CautionText] = []


class DocumentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[DocumentText] = []
    digests: list[DigestEntry] = []
    promotions: list[PromotionItem] = []


@dataclass
class _Inputs:
    key: str
    title: str
    threads: list[Thread]
    candidates: dict[str, layout.Caution]
    payload: dict[str, Any]
    fingerprint: str
    ids: set[str] = field(default_factory=set)


def _line(text: str, limit: int = MAX_LINE) -> str:
    cleaned = " ".join(unicodedata.normalize("NFC", text).split())
    return cleaned.lstrip("-*• ").strip()[:limit].rstrip()


def _tree_ref(value: str) -> str:
    return " ".join(re.sub(r"[\[\]|]", " ", value).split())


def _sha(text: str) -> str:
    return fingerprint(text)


class DocumentCompiler(Compiler):
    """One LLM call per chunk of keys; validate every bullet before writing."""

    digest: DigestBuilder | None = None
    watcher: CheckboxWatcher | None = None
    timezone: ZoneInfo = ZoneInfo("UTC")

    def __init__(
        self,
        store: WikiStore,
        fabric: Fabric,
        context: ContextDictionary,
        llm: LlmClient,
        state_path: Path,
        summary: Callable[[str], Summary],
        default_sphere: str = "회사",
        directives: Callable[[str], list[str]] = lambda key: [],
        references: Callable[[str, str], list[layout.RefLink]] | None = None,
        lifecycle_path: Path | None = None,
    ) -> None:
        super().__init__(
            store, fabric, context, llm, state_path, summary, default_sphere
        )
        self.directives = directives
        # Promotion history predates this compiler's own state file.
        self.lifecycle_path = lifecycle_path or state_path
        self.references = references or self._default_references
        with closing(sqlite3.connect(state_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS documents (key TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, title TEXT NOT NULL, "
                "bullets TEXT NOT NULL, cautions TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS dirty (key TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS doc_failures "
                "(fingerprint TEXT PRIMARY KEY, attempts INTEGER NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS kept_old (path TEXT PRIMARY KEY)")

    def _default_references(self, key: str, title: str) -> list[layout.RefLink]:
        try:
            chunks = find_references(self.state_path.parent, [key], title, limit=5)
        except (sqlite3.Error, ValueError):
            logger.warning("document references unavailable")
            return []
        links: list[layout.RefLink] = []
        for chunk in chunks:
            home = chunk.commit is None
            link = layout.RefLink(
                sphere=chunk.sphere,
                path=chunk.path.removesuffix(".md") if home else chunk.path,
                heading=_tree_ref(chunk.heading) or Path(chunk.path).stem,
                home=home,
            )
            if link not in links:
                links.append(link)
        return links

    # -- stored documents ---------------------------------------------------

    def _stored(self, key: str) -> tuple[str, str, list[BulletText], list[CautionText]]:
        with closing(sqlite3.connect(self.state_path)) as db:
            row = db.execute(
                "SELECT fingerprint, title, bullets, cautions FROM documents "
                "WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return "", "", [], []
        return (
            row[0],
            row[1],
            [BulletText.model_validate(b) for b in json.loads(row[2])],
            [CautionText.model_validate(c) for c in json.loads(row[3])],
        )

    def bullets(self, key: str) -> list[BulletText]:
        """The stored bullets of one document, in display order (b1, b2, ...)."""
        return self._stored(key)[2]

    def mark_dirty(self, key: str) -> None:
        """Recompile this document on the next run even without new Threads."""
        self._sql("INSERT OR IGNORE INTO dirty VALUES (?)", (key,))

    def _known_keys(self) -> set[str]:
        with closing(sqlite3.connect(self.state_path)) as db:
            return {key for (key,) in db.execute("SELECT key FROM documents")}

    # -- inputs -------------------------------------------------------------

    def _inputs(
        self,
        key: str,
        active: list[Thread],
        visible: list[Thread],
        dictionary: DictionarySnapshot,
    ) -> _Inputs | None:
        mine = [t for t in active if layout.page_key(t, dictionary) == key]
        # A resolution only closes a request; code renders that state, so it
        # must not change what the model sees or force another call.
        facts = [t for t in mine if not t.resolves]
        facts.sort(key=lambda t: t.seq, reverse=True)
        facts = facts[:MAX_RELATED_THREADS]
        if not facts:
            return None
        by_id = {t.thread_id: t for t in visible}
        candidates = layout.caution_candidates(mine, visible, self.timezone)
        _, stored_title, bullets, _ = self._stored(key)
        title = stored_title or layout.default_title(key)
        payload: dict[str, Any] = {
            "key": key,
            "title": title,
            "threads": [
                {
                    "id": t.thread_id,
                    "kind": t.kind,
                    "content": t.content,
                    "counterpart": t.counterpart,
                    "occurred_day": t.occurred_at.astimezone(self.timezone)
                    .date()
                    .isoformat(),
                }
                for t in facts
            ],
            "bullets": [b.model_dump() for b in bullets],
            "caution_candidates": [
                {
                    "candidate_id": c.candidate_id,
                    "label": c.label,
                    "day": c.day.isoformat(),
                    "threads": [
                        by_id[i].content or "" for i in c.thread_ids if i in by_id
                    ],
                }
                for c in candidates
            ],
            "directives": self.directives(key),
        }
        return _Inputs(
            key=key,
            title=title,
            threads=facts,
            candidates={c.candidate_id: c for c in candidates},
            payload=payload,
            # Earlier output is not new information: leave it out of the identity.
            fingerprint=fingerprint(
                {
                    "inputs": {
                        k: v
                        for k, v in payload.items()
                        if k not in ("title", "bullets")
                    },
                    "prompt": DOCUMENT_PROMPT,
                    "rules": RULES_VERSION,
                }
            ),
            ids={t.thread_id for t in facts},
        )

    # -- validation ---------------------------------------------------------

    def _accept(self, doc: DocumentText, inputs: _Inputs) -> DocumentText | None:
        bullets: list[BulletText] = []
        for bullet in doc.current:
            text = _line(bullet.text)
            ids = list(dict.fromkeys(bullet.thread_ids))
            if not text or not ids:
                logger.warning("document bullet rejected: empty text or evidence")
                continue
            if not set(ids) <= inputs.ids:
                logger.warning("document bullet rejected: foreign evidence")
                continue
            if len(" ".join(bullet.text.split())) > MAX_LINE:
                logger.warning("document bullet clipped to length limit")
            bullets.append(BulletText(text=text, thread_ids=ids))
        if not bullets:
            return None
        if len(bullets) > MAX_BULLETS:
            logger.warning("document bullets clipped to count limit")
        cautions: list[CautionText] = []
        for caution in doc.cautions:
            text = _line(caution.text)
            if (
                caution.candidate_id not in inputs.candidates
                or any(c.candidate_id == caution.candidate_id for c in cautions)
                or not text
            ):
                logger.warning("document caution rejected: unknown or empty")
                continue
            cautions.append(CautionText(candidate_id=caution.candidate_id, text=text))
        title = " ".join(unicodedata.normalize("NFC", doc.title).split())
        if not title or len(title) > MAX_TITLE or _BAD_TITLE.search(title):
            title = inputs.title
        return DocumentText(
            key=inputs.key,
            title=title,
            current=bullets[:MAX_BULLETS],
            cautions=cautions,
        )

    # -- writing ------------------------------------------------------------

    def _write(
        self,
        key: str,
        title: str,
        bullets: list[BulletText],
        cautions: list[CautionText],
        tasks: list[layout.TaskRow],
        candidates: dict[str, layout.Caution],
        thread_ids: list[str],
        source_ids: list[str],
        now: datetime,
    ) -> None:
        as_of = now.astimezone(self.timezone).date()
        shown = [
            (candidates[c.candidate_id], c.text)
            for c in cautions
            if c.candidate_id in candidates
        ]
        body = layout.render_document(
            title,
            as_of,
            [
                layout.Bullet(f"b{i}", b.text, tuple(b.thread_ids))
                for i, b in enumerate(bullets, 1)
            ],
            layout.remaining(key, tasks),
            shown,
            self.references(key, title),
            self.timezone,
        )
        path = f"pages/{key}.md"
        frontmatter: dict[str, JsonValue] = {
            "title": title,
            "page_key": key,
            "kind": node_kind(key),
            "as_of": as_of.isoformat(),
            "source_ids": list[JsonValue](source_ids),
        }
        try:
            _, _, base = self.store.read(path)
            if not self.store.is_tracked_version(path, base):
                raise HumanEditPending(path)
            if (
                self.store.render(path, frontmatter, body, readonly=True)[0]
                == (self.store.root / path).read_bytes()
                and self.store.metadata(path).get("page_key") == key
            ):
                return
        except FileNotFoundError:
            base = None
        self.store.write(
            path, frontmatter, body, base, metadata="document update", readonly=True
        )
        self.store.write_metadata(
            path,
            {
                "bullets": [
                    {
                        "bullet_id": f"b{i}",
                        "section": "current",
                        "text_sha256": _sha(b.text),
                        "thread_ids": list[JsonValue](b.thread_ids),
                    }
                    for i, b in enumerate(bullets, 1)
                ],
                "cautions": [
                    {
                        "candidate_id": c.candidate_id,
                        # Shown date, label and text let readers match file lines
                        # to evidence without relying on line order.
                        "date": candidates[c.candidate_id].day.isoformat(),
                        "label": candidates[c.candidate_id].label,
                        "text": c.text,
                        "text_sha256": _sha(c.text),
                        "thread_ids": list[JsonValue](
                            candidates[c.candidate_id].thread_ids
                        ),
                    }
                    for c in cautions
                    if c.candidate_id in candidates
                ],
                "thread_ids": list[JsonValue](thread_ids),
                "page_key": key,
                "title": title,
            },
        )

    # -- run ----------------------------------------------------------------

    @staticmethod
    def _targets(t: Thread) -> list[str]:
        found = [
            t.correction.target if t.correction else None,
            t.retracts,
            t.executes,
            t.resolves,
            t.amends,
            t.updates,
        ]
        return [x for x in found if x]

    def _touched(
        self, new: Iterable[Thread], visible: list[Thread], d: DictionarySnapshot
    ) -> set[str]:
        by_id = {t.thread_id: t for t in visible}
        keys: set[str] = set()
        for t in new:
            queue, seen = [t], set[str]()
            while queue:
                current = queue.pop()
                if current.thread_id in seen:
                    continue
                seen.add(current.thread_id)
                key = layout.page_key(current, d)
                if key:
                    keys.add(key)
                queue += [by_id[i] for i in self._targets(current) if i in by_id]
        return keys

    # -- bookkeeping --------------------------------------------------------

    def _sql(self, query: str, args: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.state_path)) as db, db:
            return db.execute(query, args).fetchall()

    def _writable(self, key: str) -> bool:
        """Whether a write would pass now, checked before any model call."""
        path = f"pages/{key}.md"
        try:
            _, _, base = self.store.read(path)
        except FileNotFoundError:
            base = None
        try:
            if base is not None and not self.store.is_tracked_version(path, base):
                return False
            self.store.check_write(path, base)
        except HumanEditPending:
            return False
        return True

    def _retire_old_pages(self) -> None:
        """Retire flat pages and index files an earlier BatchCompiler wrote.

        Only files still identical to what Weaver wrote are removed; anything a
        person edited stays and is logged once.
        """
        found = sorted(
            p.relative_to(self.store.root).as_posix()
            for p in (self.store.root / "pages").glob("*.md")
        )
        found += [p for p in _OLD_ROOT_PAGES if (self.store.root / p).exists()]
        kept = {row[0] for row in self._sql("SELECT path FROM kept_old")}
        for path in found:
            path = unicodedata.normalize("NFC", path)
            try:
                _, _, digest = self.store.read(path)
                if not self.store.is_tracked_version(path, digest):
                    raise HumanEditPending(path)
                self.store.retire_page(path, digest)
            except (FileNotFoundError, HumanEditPending):
                if path not in kept:
                    logger.warning("old wiki page kept: edited by a person")
                    self._sql("INSERT OR IGNORE INTO kept_old VALUES (?)", (path,))

    def _store_result(self, inputs: _Inputs, doc: DocumentText) -> None:
        self._sql(
            "INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?)",
            (
                inputs.key,
                inputs.fingerprint,
                doc.title,
                json.dumps([b.model_dump() for b in doc.current], ensure_ascii=False),
                json.dumps([c.model_dump() for c in doc.cautions], ensure_ascii=False),
            ),
        )

    # -- run ----------------------------------------------------------------

    def _cap_dirty(self, keys: set[str], inputs: dict[str, _Inputs]) -> None:
        """Stop retrying a dirty key whose input never yields a valid document."""
        for key in keys:
            identity = inputs[key].fingerprint
            self._sql(
                "INSERT INTO doc_failures VALUES (?, 1) "
                "ON CONFLICT(fingerprint) DO UPDATE SET attempts=attempts+1",
                (identity,),
            )
            attempts = self._sql(
                "SELECT attempts FROM doc_failures WHERE fingerprint=?", (identity,)
            )[0][0]
            if attempts >= MAX_ATTEMPTS:
                logger.warning("wiki document dropped from retry: no valid bullets")
                self._sql("DELETE FROM dirty WHERE key=?", (key,))

    def _call(
        self,
        batch: list[_Inputs],
        targets: list[Any],
    ) -> DocumentBatch | None:
        """One model call; a deterministic failure is retried once, then skipped."""
        identity = fingerprint(
            {
                "documents": sorted(i.fingerprint for i in batch),
                "digests": sorted(t.fingerprint for t in targets),
            }
        )
        row = self._sql(
            "SELECT attempts FROM doc_failures WHERE fingerprint=?", (identity,)
        )
        if row and row[0][0] >= MAX_ATTEMPTS:
            logger.warning("wiki documents skipped: repeated failure")
            return None
        payload = {
            "rules_version": RULES_VERSION,
            "documents": [i.payload for i in batch],
            "digest_targets": [t.payload() for t in targets],
        }
        try:
            return self.llm.generate_json(
                "wiki.compile_documents",
                DOCUMENT_PROMPT,
                json.dumps(payload, ensure_ascii=False),
                DocumentBatch,
            )
        except _CONTENT_FAILURES as error:
            self._sql(
                "INSERT INTO doc_failures VALUES (?, 1) "
                "ON CONFLICT(fingerprint) DO UPDATE SET attempts=attempts+1",
                (identity,),
            )
            attempts = self._sql(
                "SELECT attempts FROM doc_failures WHERE fingerprint=?", (identity,)
            )[0][0]
            logger.warning("wiki documents failed: %s", type(error).__name__)
            if attempts < MAX_ATTEMPTS:
                raise
            logger.warning("wiki documents skipped: repeated failure")
            return None

    def _run(self) -> None:
        if self.watcher is not None:
            # Record ticks before the file is restored and tasks are re-rendered.
            try:
                self.watcher.collect(
                    canonical_threads(self.fabric.entries(ThreadQuery()))
                )
            except Exception:
                logger.exception("Wiki checkbox collection failed")
            self.store.restore_readonly(on_change=self.watcher.on_change)
        else:
            self.store.restore_readonly()
        self._retire_old_pages()
        lifecycle = ProjectLifecycle(
            self.lifecycle_path,
            self.context,
            self.fabric,
            self.timezone,
            self.default_sphere,
        )
        now = datetime.fromtimestamp(self.store.clock(), UTC)
        lifecycle.refresh(now)
        threads = canonical_threads(self.fabric.entries(ThreadQuery()))
        bookmark = self.bookmark()
        new = sorted(
            (
                t
                for t in threads
                if t.seq > bookmark
                and not t.thread_id.startswith(PROJECT_THREAD_PREFIX)
            ),
            key=lambda t: t.seq,
        )[:MAX_NEW_THREADS]
        dirty = {row[0] for row in self._sql("SELECT key FROM dirty")}
        if not new and not dirty:
            self._sync_pages()
            return
        high = max((t.seq for t in new), default=bookmark)
        visible = [
            t
            for t in threads
            if t.seq <= high or t.thread_id.startswith(PROJECT_THREAD_PREFIX)
        ]
        retracted = {t.retracts for t in visible if t.retracts}
        active = [
            t
            for t in visible
            if t.thread_id not in retracted
            and not t.retracts
            and t.kind not in _EXCLUDED
            and not t.thread_id.startswith(PROJECT_THREAD_PREFIX)
        ]
        dictionary = self.context.snapshot()
        touched = self._touched(new, visible, dictionary) | dirty
        render_keys = touched | (self._known_keys() if new else set())
        inputs: dict[str, _Inputs] = {}
        for key in sorted(render_keys):
            found = self._inputs(key, active, visible, dictionary)
            if found is not None:
                inputs[key] = found
        for key in dirty - inputs.keys():
            self._sql("DELETE FROM dirty WHERE key=?", (key,))
        # A file inside its settle window (or with a person's edit) cannot be
        # written now. Decide that before paying for a model call and retry the
        # key later from the dirty list.
        blocked = {key for key in inputs if not self._writable(key)}
        for key in blocked:
            self._sql("INSERT OR IGNORE INTO dirty VALUES (?)", (key,))
        pending = [
            i
            for key, i in inputs.items()
            if key in touched
            and key not in blocked
            and self._stored(key)[0] != i.fingerprint
        ]
        stable = lifecycle.stable_threads(visible)
        digest_targets = self.digest.plan(stable, high) if self.digest and new else []
        asked: set[str] = set()
        chunks = [
            pending[n : n + MAX_KEYS_PER_CALL]
            for n in range(0, len(pending), MAX_KEYS_PER_CALL)
        ]
        if digest_targets and not chunks:
            chunks = [[]]
        withdrawn = {t.retracts for t in threads if t.retracts}
        full_active = [
            t
            for t in threads
            if t.thread_id not in withdrawn
            and not t.retracts
            and t.kind not in _EXCLUDED
        ]
        for n, batch in enumerate(chunks):
            targets = digest_targets if n == 0 else []
            result = self._call(batch, targets)
            asked.update(i.key for i in batch)
            if result is None:
                # Digest targets of a skipped chunk are not requeued: they are
                # planned again from stored fingerprints when a later run has
                # new Threads.
                continue
            by_key = {i.key: i for i in batch}
            seen: set[str] = set()
            for item in result.documents:
                target = by_key.get(item.key)
                if target is None or item.key in seen:
                    logger.warning("document rejected: unknown or duplicate key")
                    continue
                seen.add(item.key)
                checked = self._accept(item, target)
                if checked is None:
                    logger.warning("document kept: no valid bullets")
                    continue
                # Persist before writing files so a failed write replays free.
                self._store_result(target, checked)
                asked.discard(item.key)
            if self.digest is not None and targets:
                self.digest.apply(targets, result.digests, high)
            lifecycle.apply(
                result.promotions,
                full_active,
                now,
                replay=False,
                proposals=self.location_proposals,
            )
        self._cap_dirty(asked & dirty, inputs)
        today = now.astimezone(self.timezone).date()
        tasks = layout.open_tasks(decorate(visible), dictionary, today)
        for key, found in inputs.items():
            if key in blocked or key in asked:
                # Nothing valid came back (or the file is busy): keep the old file.
                continue
            _, title, bullets, cautions = self._stored(key)
            if not bullets:
                continue
            try:
                self._write(
                    key,
                    title,
                    bullets,
                    cautions,
                    tasks,
                    found.candidates,
                    [t.thread_id for t in found.threads],
                    sorted({t.source_id for t in found.threads}),
                    now,
                )
            except HumanEditPending:
                self._sql("INSERT OR IGNORE INTO dirty VALUES (?)", (key,))
            else:
                self._sql("DELETE FROM dirty WHERE key=?", (key,))
        # Nested documents are not synced as dictionary pages: the vault tree
        # already supplies those entries. Only leftover flat pages are seen.
        self._sync_pages()
        self._sql("UPDATE bookmark SET seq=?", (high,))
        lifecycle.refresh(now)

    def refresh_index(self) -> None:
        """Navigation is the document tree itself; nothing to publish yet."""
        return None
