"""Periodic consumers of the Fabric log."""

import logging
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from weaver.context import ContextDictionary
from weaver.fabric import Fabric, ThreadQuery
from weaver.llm.types import LlmClient
from weaver.wiki.batch import BatchCompiler
from weaver.wiki.digest import DigestBuilder
from weaver.wiki.links import rewrite_links, summary_path
from weaver.wiki.queries import write_pages
from weaver.wiki.store import HumanEditPending, WikiStore

from .progress import PipelineJournal
from .summary import SummaryLookup
from .wiki_progress import WikiEditPublisher

logger = logging.getLogger(__name__)


class WikiJob:
    """Run independent consumers, retrying protected pages on the next interval."""

    def __init__(
        self,
        store: WikiStore,
        fabric: Fabric,
        context: ContextDictionary,
        client: LlmClient,
        state: Path,
        zone: ZoneInfo,
        lookup: SummaryLookup,
        journal: PipelineJournal | None = None,
    ) -> None:
        self.store, self.fabric, self.context, self.zone, self.lookup = (
            store,
            fabric,
            context,
            zone,
            lookup,
        )
        self.compiler = BatchCompiler(
            store, fabric, context, client, state / "wiki-compiler.sqlite3", lookup
        )
        self.digest = DigestBuilder(
            store,
            state / "wiki-digest.sqlite3",
            zone,
            self.source_link,
            lambda error: self._deferred("digest", error),
        )
        self.compiler.digest = self.digest
        self._waiting: dict[str, tuple[str, float | None]] = {}
        self.observer = (
            WikiEditPublisher(journal, store, self.compiler, fabric, store.vault)
            if journal
            else None
        )
        if self.observer is not None:
            self.compiler.on_progress = self.observer.publish
        self.state_path = state / "wiki-queries.sqlite3"
        with sqlite3.connect(self.state_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bookmark (id INTEGER PRIMARY KEY, seq INTEGER NOT NULL)"
            )

    def source_link(self, source_id: str) -> str | None:
        try:
            title = self.lookup(source_id).title
        except (ValueError, LookupError, FileNotFoundError):
            return None
        return summary_path(title, source_id).removesuffix(".md")

    def _copy_summaries(self) -> None:
        replacements = {}
        retired: dict[str, str] = {}
        threads = self.fabric.entries(ThreadQuery())
        required = {t.source_id for t in threads if t.actor == "ai_approved"}
        for source_id in sorted({t.source_id for t in threads}):
            try:
                summary = self.lookup(source_id)
            except (ValueError, LookupError, FileNotFoundError):
                if source_id in required:
                    raise
                continue
            path = summary_path(summary.title, source_id)
            legacy_title = summary.title.replace("/", "-").replace("\\", "-")
            legacy = f"summaries/{legacy_title} {source_id}"
            replacements[legacy] = path.removesuffix(".md")
            replacements[legacy + ".md"] = path
            try:
                _, body, base = self.store.read(path)
                if body.rstrip("\n") != summary.body.rstrip(
                    "\n"
                ) or not self.store.is_tracked_version(path, base):
                    self.store.write(path, {}, summary.body, base, readonly=True)
            except FileNotFoundError:
                self.store.write(path, {}, summary.body, None, readonly=True)

            for candidate in (self.store.root / "summaries").glob("*.md"):
                old = unicodedata.normalize(
                    "NFC", candidate.relative_to(self.store.root).as_posix()
                )
                if old == path or not old.endswith(f" {source_id}.md"):
                    continue
                _, old_body, digest = self.store.read(old)
                # Retire obsolete approved versions, but keep unrecorded edits.
                if not self.store.is_tracked_version(old, digest) and (
                    old_body.rstrip("\n") != summary.body.rstrip("\n")
                ):
                    continue
                replacements[old] = path
                replacements[old.removesuffix(".md")] = path.removesuffix(".md")
                retired[old] = digest

        self.store.repair_links(replacements)
        for old, digest in retired.items():
            mapping = {
                key: value
                for key, value in replacements.items()
                if key in (old, old.removesuffix(".md"))
            }
            # A page being edited may still link here. Retry after it settles.
            if any(
                rewrite_links(page.read_text(), mapping) != page.read_text()
                for page in self.store.root.rglob("*.md")
                if ".stversions" not in page.relative_to(self.store.root).parts
                and unicodedata.normalize(
                    "NFC", page.relative_to(self.store.root).as_posix()
                )
                != old
            ):
                continue
            try:
                self.store.retire_summary(old, digest)
            except (FileNotFoundError, HumanEditPending):
                continue

    def repair_source_links(self) -> None:
        """Copy safe source notes and repair stable links without calling an LLM."""
        self._copy_summaries()

    def _deferred(self, consumer: str, error: HumanEditPending) -> None:
        status = (str(error), error.ready_at)
        if self._waiting.get(consumer) != status:
            remaining = (
                max(0.0, error.ready_at - self.store.clock())
                if error.ready_at is not None
                else None
            )
            logger.info(
                "Wiki consumer deferred: consumer=%s count=1 remaining_seconds=%s",
                consumer,
                "?" if remaining is None else f"{remaining:.0f}",
            )
            self._waiting[consumer] = status

    def run(self) -> None:
        try:
            self._run()
        finally:
            if self.observer is not None:
                self.observer.publish()

    def _run(self) -> None:
        try:
            self.repair_source_links()
            self.compiler.run()
        except HumanEditPending as error:
            self._deferred("compiler", error)
            return
        self._waiting.pop("compiler", None)
        if not self.digest.pending:
            self._waiting.pop("digest", None)
        try:
            self._copy_summaries()
        except HumanEditPending as error:
            self._deferred("digest", error)
        with sqlite3.connect(self.state_path) as db:
            row = db.execute("SELECT seq FROM bookmark WHERE id=1").fetchone()
            new = self.fabric.entries(ThreadQuery(after_seq=row[0] if row else 0))
        try:
            # Calendar views also change when the date changes without new threads.
            write_pages(
                self.fabric,
                self.context,
                self.store,
                now=datetime.now(self.zone),
                source_link=self.source_link,
            )
        except HumanEditPending as error:
            self._deferred("queries", error)
            return
        self._waiting.pop("queries", None)
        with sqlite3.connect(self.state_path) as db:
            if new:
                db.execute(
                    "INSERT OR REPLACE INTO bookmark VALUES (1, ?)",
                    (max(t.seq for t in new),),
                )
        try:
            self.compiler.refresh_index()
        except HumanEditPending as error:
            self._deferred("index", error)
        else:
            self._waiting.pop("index", None)
