"""Periodic consumers of the Fabric log."""

import logging
import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from weaver.context import ContextDictionary
from weaver.context.proposals import LocationProposals
from weaver.fabric import Fabric, ThreadQuery
from weaver.llm.errors import LlmError
from weaver.llm.types import LlmClient
from weaver.schema import Alert
from weaver.wiki.batch import BatchCompiler
from weaver.wiki.checkbox import CheckboxWatcher
from weaver.wiki.digest import DigestBuilder
from weaver.wiki.documents import DocumentCompiler
from weaver.wiki.feedback import FeedbackProcessor, FeedbackStore
from weaver.wiki.links import rewrite_links, summary_path
from weaver.wiki.queries import write_pages
from weaver.wiki.snapshot import canonical_threads
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
        default_sphere: str = "회사",
        location_proposals: LocationProposals | None = None,
        publish_alert: Callable[[Alert], object] | None = None,
    ) -> None:
        self.store, self.fabric, self.context, self.zone, self.lookup = (
            store,
            fabric,
            context,
            zone,
            lookup,
        )
        # A vault tree selects the bundled PARA documents; older installs keep
        # the section-based pages.
        legacy_state = state / "wiki-compiler.sqlite3"
        self.compiler: BatchCompiler | DocumentCompiler
        if context.snapshot().has_tree:
            # Own state and bookmark from 0, so the backlog compiles in chunks.
            self.compiler = DocumentCompiler(
                store,
                fabric,
                context,
                client,
                state / "wiki-documents.sqlite3",
                lookup,
                default_sphere,
                lifecycle_path=legacy_state,
            )
        else:
            self.compiler = BatchCompiler(
                store, fabric, context, client, legacy_state, lookup, default_sphere
            )
        self.feedback: FeedbackProcessor | None = None
        if isinstance(self.compiler, DocumentCompiler):
            feedback_store = FeedbackStore(state / "wiki-feedback.sqlite3")
            # A saved directive changes the document's compile fingerprint.
            self.compiler.directives = feedback_store.directives
            self.feedback = FeedbackProcessor(
                self.compiler, feedback_store, fabric, client
            )
        self.compiler.timezone = zone
        self.compiler.location_proposals = location_proposals
        self.digest = DigestBuilder(
            store,
            state / "wiki-digest.sqlite3",
            zone,
            self.source_link,
            lambda error: self._deferred("digest", error),
            source_label=self.source_label,
            dictionary=context.snapshot,
        )
        self.compiler.digest = self.digest
        self.watcher = CheckboxWatcher(
            store, fabric, state / "wiki-checkbox.sqlite3", zone, publish_alert
        )
        if isinstance(self.compiler, DocumentCompiler):
            self.compiler.watcher = self.watcher
        self._waiting: dict[str, tuple[str, float | None]] = {}
        self._feedback_error: LlmError | None = None
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

    def source_label(self, source_id: str) -> str | None:
        try:
            return self.lookup(source_id).title
        except (ValueError, LookupError, FileNotFoundError):
            return None

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
        self._feedback_error = None
        try:
            self._run()
        finally:
            if self.observer is not None:
                self.observer.publish()
        if self._feedback_error is not None:
            # Reach the loop's gate and backoff once the other consumers ran.
            raise self._feedback_error

    def _process_feedback(self) -> None:
        if self.feedback is None:
            return
        try:
            self.feedback.process()
        except LlmError as error:
            logger.warning("Wiki feedback deferred: %s", error.code)
            self._feedback_error = error
        except Exception:
            # Feedback must never keep the documents from compiling.
            logger.exception("Wiki feedback failed")

    def _run(self) -> None:
        # Ticks are recorded before any consumer restores or re-renders the file.
        # DocumentCompiler repeats this; kept for the legacy BatchCompiler path,
        # which restores read-only pages without the watcher.
        try:
            self.watcher.collect(canonical_threads(self.fabric.entries(ThreadQuery())))
        except Exception:
            # A bad hand-edit must not stall every consumer.
            logger.exception("Wiki checkbox collection failed")
        self.store.restore_readonly(on_change=self.watcher.on_change)
        try:
            self.repair_source_links()
            self._process_feedback()
            self.compiler.run()
        except HumanEditPending as error:
            self._deferred("compiler", error)
            return
        self._waiting.pop("compiler", None)
        try:
            # Checkbox state changes without a model call, even with no new threads.
            rows = self.watcher.collect(
                canonical_threads(self.fabric.entries(ThreadQuery()))
            )
            if rows:
                self.digest.refresh_tasks(rows, max(t.seq for t in rows))
        except HumanEditPending as error:
            self._deferred("digest", error)
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
                source_label=self.source_label,
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
