"""Periodic consumers of the Fabric log."""

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from weaver.context import ContextDictionary
from weaver.fabric import Fabric, ThreadQuery
from weaver.llm.types import LlmClient
from weaver.wiki.compiler import Compiler
from weaver.wiki.digest import DigestBuilder
from weaver.wiki.queries import write_pages
from weaver.wiki.store import HumanEditPending, WikiStore

from .summary import SummaryLookup


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
    ) -> None:
        self.store, self.fabric, self.context, self.zone, self.lookup = (
            store,
            fabric,
            context,
            zone,
            lookup,
        )
        self.compiler = Compiler(
            store, fabric, context, client, state / "wiki-compiler.sqlite3", lookup
        )
        self.digest = DigestBuilder(
            fabric, store, client, state / "wiki-digest.sqlite3", zone, self.source_link
        )
        self.state_path = state / "wiki-queries.sqlite3"
        with sqlite3.connect(self.state_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bookmark (id INTEGER PRIMARY KEY, seq INTEGER NOT NULL)"
            )

    def source_link(self, source_id: str) -> str:
        try:
            title = self.lookup.title(source_id).replace("/", "-").replace("\\", "-")
        except (ValueError, LookupError, FileNotFoundError):
            return source_id
        return f"summaries/{title} {source_id}"

    def _copy_summaries(self) -> None:
        for source_id in {
            t.source_id
            for t in self.fabric.entries(ThreadQuery())
            if t.actor == "ai_approved"
        }:
            summary = self.lookup(source_id)
            path = self.source_link(source_id) + ".md"
            try:
                _, body, base = self.store.read(path)
                if body.rstrip("\n") == summary.body.rstrip(
                    "\n"
                ) and self.store.is_tracked_version(path, base):
                    continue
            except FileNotFoundError:
                base = None
            self.store.write(path, {}, summary.body, base, readonly=True)

    def run(self) -> None:
        try:
            self.compiler.run()
        except HumanEditPending:
            return
        try:
            self._copy_summaries()
            self.digest.update()
        except HumanEditPending:
            pass
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
        except HumanEditPending:
            return
        with sqlite3.connect(self.state_path) as db:
            if new:
                db.execute(
                    "INSERT OR REPLACE INTO bookmark VALUES (1, ?)",
                    (max(t.seq for t in new),),
                )
