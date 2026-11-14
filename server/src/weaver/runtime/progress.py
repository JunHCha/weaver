"""Durable, additive phone observations; never infer success from file deletion.

Each source has a monotonic revision and a complete location history. The private
journal is authoritative, while proposals/pipeline is a republishable projection.
Missing historic stage times remain missing rather than being invented.
"""

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from weaver.archive import ArchiveStore
from weaver.courier.atomic import write_bytes_atomic
from weaver.fabric import Fabric, ThreadQuery
from weaver.loom.summary_review import Prepared
from weaver.loom.summary_state import SummaryState
from weaver.schema import (
    PipelineItem,
    PipelinePage,
    PipelineThread,
    ReviewPackage,
    Source,
    Verdict,
)
from weaver.wiki.store import WikiStore

logger = logging.getLogger(__name__)


def observation[**P](operation: Callable[P, None]) -> Callable[P, None]:
    """Sidecar failures must not turn a committed approval into an apply failure."""

    @wraps(operation)
    def attempt(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            operation(*args, **kwargs)
        except Exception as error:
            logger.warning("pipeline observation failed: %s", type(error).__name__)

    return attempt


class PipelineJournal:
    def __init__(self, state: Path, proposals: Path, *, failure_limit: int = 5) -> None:
        if failure_limit < 1:
            raise ValueError("pipeline failure limit must be positive")
        self.failure_limit = failure_limit
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = state / "pipeline.sqlite3"
        self.directory = proposals / "pipeline"
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS items (source_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
        self.path.chmod(0o600)

    def get(self, source_id: str) -> PipelineItem:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM items WHERE source_id=?", (source_id,)
            ).fetchone()
        if row is None:
            raise LookupError("unknown pipeline source")
        return PipelineItem.model_validate_json(row[0])

    def receive(self, source: Source, received_at: datetime) -> PipelineItem:
        try:
            return self.get(source.source_id)
        except LookupError:
            pass
        item = PipelineItem(
            schema_version=2,
            revision=1,
            capture_id=source.capture_id,
            source_id=source.source_id,
            package_id=None,
            package_revision=None,
            source_type=source.channel,
            title="수확한 데이터",
            location="spinner",
            status="working",
            apply_failure_limit=self.failure_limit,
            stage_times={"수집": source.captured_at, "전송": received_at},
            history=["spinner"],
            threads=[],
            pages=[],
            digest=None,
            updated_at=received_at,
        )
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR IGNORE INTO items VALUES (?, ?)",
                (item.source_id, item.model_dump_json()),
            )
        item = self.get(source.source_id)
        self._write(item)
        return item

    def receive_wiki_edit(self, item: PipelineItem) -> PipelineItem:
        """Wiki-origin observations have no capture receipt or review package."""
        if item.wiki_edit is None:
            raise ValueError("expected Wiki edit observation")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR IGNORE INTO items VALUES (?, ?)",
                (item.source_id, item.model_dump_json()),
            )
        return self.get(item.source_id)

    def wiki_edits(self) -> list[PipelineItem]:
        with sqlite3.connect(self.path) as db:
            items = [
                PipelineItem.model_validate_json(row[0])
                for row in db.execute("SELECT payload FROM items")
            ]
        return [item for item in items if item.wiki_edit is not None]

    def update(self, source_id: str, **changes: Any) -> PipelineItem:
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM items WHERE source_id=?", (source_id,)
            ).fetchone()
            if row is None:
                raise LookupError("unknown pipeline source")
            current = PipelineItem.model_validate_json(row[0])
            values = current.model_dump()
            values.update(changes)
            if values["location"] != current.location:
                values["history"] = [*current.history, values["location"]]
            item = PipelineItem.model_validate(values)
            if item == current:
                return current
            item = item.model_copy(
                update={
                    "revision": current.revision + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            db.execute(
                "UPDATE items SET payload=? WHERE source_id=?",
                (item.model_dump_json(), source_id),
            )
        self._write(item)
        return item

    def _write(self, item: PipelineItem) -> None:
        if self.directory.is_symlink():
            raise OSError("pipeline projection directory is a symlink")
        self.directory.mkdir(parents=True, exist_ok=True)
        final = self.directory / f"{item.source_id}.json"
        payload = item.model_dump(mode="json")
        if item.wiki_edit is None:
            # Existing strict v2 clients continue to read capture observations.
            payload.pop("wiki_edit", None)
            for thread in payload["threads"]:
                thread.pop("content", None)
        data = json.dumps(payload, ensure_ascii=False).encode()
        if final.is_symlink() or not final.is_file() or final.read_bytes() != data:
            write_bytes_atomic(final, data)

    def publish(self) -> None:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT payload FROM items").fetchall()
        for (raw,) in rows:
            self._write(PipelineItem.model_validate_json(raw))


class PipelinePublisher:
    """Recover observations from journals, then reconcile committed wiki outputs."""

    def __init__(
        self,
        journal: PipelineJournal,
        archive: ArchiveStore,
        state: SummaryState,
        fabric: Fabric,
        proposals: Path,
    ) -> None:
        self.journal, self.archive, self.state, self.fabric, self.proposals = (
            journal,
            archive,
            state,
            fabric,
            proposals,
        )
        self.wiki: WikiStore | None = None
        self.vault: Path | None = None

    def receive(self, source: Source) -> PipelineItem:
        captures = self.archive.captures_for(source.source_id)
        return self.journal.receive(source, captures[0].receipt.received_at)

    @observation
    def begin(self, source: Source) -> None:
        self.receive(source)

    @observation
    def organized(self, source: Source) -> None:
        self.journal.update(source.source_id, location="loom", status="working")

    @observation
    def observe_package(self, package: ReviewPackage) -> None:
        try:
            source = self.archive.get(package.source_id)
        except LookupError:
            return  # old/manual packages may not have an indexed capture
        old = self.receive(source)
        self.journal.update(
            package.source_id,
            package_id=package.package_id,
            package_revision=package.revision,
            title=package.title or old.title,
            location="user",
            status="review",
            stage_times=old.stage_times | package.stage_times,
        )

    @observation
    def applying(self, verdict: Verdict) -> None:
        package, _ = self.state.load_package(verdict.package_id, verdict.revision)
        try:
            self.receive(self.archive.get(package.source_id))
        except LookupError:
            return
        current = self.journal.get(package.source_id)
        if (
            current.status == "apply_stopped"
            and current.package_revision == package.revision
        ):
            return  # retrying/restarting is not evidence of recovery
        self.journal.update(package.source_id, location="loom", status="applying")

    @observation
    def stopped(self, verdict: Verdict) -> None:
        package, _ = self.state.load_package(verdict.package_id, verdict.revision)
        try:
            self.receive(self.archive.get(package.source_id))
        except LookupError:
            return
        self.journal.update(
            package.source_id,
            location="loom",
            status="apply_stopped",
            apply_failure_limit=self.journal.failure_limit,
        )

    def publish(self) -> None:
        for sid, raw in self.archive.source_rows():
            try:
                source = Source.model_validate_json(raw)
                self.receive(source)
                self._reconcile(source)
            except (ValueError, LookupError, OSError, sqlite3.Error):
                logger.warning("pipeline observation unavailable: %s", sid)
        self.journal.publish()

    def _reconcile(self, source: Source) -> None:
        with self.state.connect() as db:
            rows = db.execute(
                "SELECT package FROM packages ORDER BY revision DESC"
            ).fetchall()
            packages = [ReviewPackage.model_validate_json(row[0]) for row in rows]
            package = next(
                (p for p in packages if p.source_id == source.source_id), None
            )
            if package is None:
                return
            review = db.execute(
                "SELECT stage, verdict, prepared FROM reviews WHERE id=? AND revision=?",
                (package.package_id, package.revision),
            ).fetchone()
        current = self.journal.get(source.source_id)
        values: dict[str, Any] = {
            "package_id": package.package_id,
            "package_revision": package.revision,
            "title": package.title or current.title,
            "stage_times": current.stage_times | package.stage_times,
        }
        if review is not None and review[0] == "APPLIED":
            verdict = Verdict.model_validate_json(review[1])
            prepared = Prepared.model_validate_json(review[2])
            if verdict.action == "feedback":
                return  # new revision was journaled atomically by SummaryReview
            if prepared.applied_at is not None:
                values["stage_times"]["반영"] = prepared.applied_at
            threads = [
                t
                for t in self.fabric.entries(ThreadQuery())
                if t.source_id == source.source_id
            ]
            values.update(
                location="fabric",
                status="rejected" if verdict.action == "reject" else "done",
                threads=[
                    PipelineThread(thread_id=t.thread_id, kind=t.kind) for t in threads
                ],
            )
            if current.location == "wiki":
                values["location"] = "wiki"
        elif (
            current.status in {"apply_stopped", "applying"}
            and current.package_revision == package.revision
        ):
            pass  # restarting is not evidence of recovery; only a successful apply clears it
        elif review is not None and review[0] == "PREPARED":
            values.update(location="loom", status="applying")
        else:
            values.update(location="user", status="review")
        self.journal.update(source.source_id, **values)
        if review is not None and review[0] == "APPLIED":
            self._pages(source.source_id)

    def _pages(self, source_id: str) -> None:
        if self.wiki is None or self.vault is None:
            return
        item = self.journal.get(source_id)
        prior = {
            p.path: p for p in [*item.pages, *([item.digest] if item.digest else [])]
        }
        pages_by_path = {
            p.path: p
            for p in item.pages
            if (self.vault / p.path).is_file()
            and not (self.vault / p.path).is_symlink()
        }
        digest: PipelinePage | None = item.digest
        if digest is not None and not (self.vault / digest.path).is_file():
            digest = None
        prefix = self.wiki.root.relative_to(self.vault).as_posix()
        for relative, data, timestamp in self.wiki.published_pages():
            if source_id.encode() not in data and source_id not in relative:
                continue
            path = f"{prefix}/{relative}" if prefix != "." else relative
            sha = hashlib.sha256(data).hexdigest()
            old = prior.get(path)
            page = (
                old
                if old is not None and old.sha256 == sha
                else PipelinePage(path=path, updated_at=timestamp, sha256=sha)
            )
            if relative.startswith("Digest/"):
                # Weekly/monthly outputs stay outside the map's daily Digest entry.
                if (
                    relative.split("/")[-1][:4].isdigit()
                    and len(Path(relative).stem) == 10
                ):
                    if digest is None or page.updated_at > digest.updated_at:
                        digest = page
            else:
                pages_by_path[path] = page
        pages = list(pages_by_path.values())
        if not pages and digest is None:
            self.journal.update(source_id, pages=[], digest=None)
            return
        latest = max(p.updated_at for p in [*pages, *([digest] if digest else [])])
        self.journal.update(
            source_id,
            pages=sorted(pages, key=lambda p: p.path),
            digest=digest,
            location="wiki",
            stage_times=item.stage_times | {"wiki갱신": latest},
        )
