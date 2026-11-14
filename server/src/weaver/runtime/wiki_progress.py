"""Recover map observations for Wiki-origin edits without manufacturing captures."""

import hashlib
import json
import sqlite3
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from weaver.fabric import Fabric, ThreadQuery
from weaver.schema import (
    PipelineItem,
    PipelinePage,
    PipelineThread,
    PipelineWikiChange,
    PipelineWikiEdit,
    ThreadDraft,
)
from weaver.wiki.compiler import Compiler
from weaver.wiki.store import WikiEditState, WikiStore

from .progress import PipelineJournal, observation


def wiki_edit_source(path: str, base_rev: str) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    data = unicodedata.normalize("NFC", path) + "\0" + base_rev
    number = int(hashlib.sha256(data.encode()).hexdigest()[:32], 16)
    return "src_" + "".join(
        alphabet[(number >> (5 * i)) & 31] for i in reversed(range(26))
    )


def _timestamp(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value is not None else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    return (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )


class WikiEditPublisher:
    def __init__(
        self,
        journal: PipelineJournal,
        store: WikiStore,
        compiler: Compiler,
        fabric: Fabric,
        vault: Path,
    ) -> None:
        self.journal, self.store, self.compiler, self.fabric, self.vault = (
            journal,
            store,
            compiler,
            fabric,
            vault,
        )

    def _drafts(self, state: WikiEditState) -> list[ThreadDraft] | None:
        if state.edit is None:
            return None
        with sqlite3.connect(self.compiler.state_path) as db:
            row = db.execute(
                "SELECT drafts FROM edits WHERE rev=?", (state.edit.new_rev,)
            ).fetchone()
        return (
            [ThreadDraft.model_validate(item) for item in json.loads(row[0])]
            if row
            else None
        )

    @observation
    def publish(self) -> None:
        prefix = unicodedata.normalize(
            "NFC", self.store.root.relative_to(self.vault).as_posix()
        )
        now = datetime.fromtimestamp(self.store.clock(), UTC)
        threads = {t.thread_id: t for t in self.fabric.entries(ThreadQuery())}
        published = self.store.published_pages()
        active = set()
        for state in self.store.edit_states():
            source_id = wiki_edit_source(state.path, state.base_rev)
            active.add(source_id)
            path = f"{prefix}/{state.path}" if prefix != "." else state.path
            drafts = self._drafts(state)
            changes = [
                PipelineWikiChange(
                    field=field,
                    before=_text(state.before.get(field)),
                    after=_text(state.after.get(field)),
                )
                for field in ("deadline", "schedule", "assignees", "status")
                if state.before.get(field) != state.after.get(field)
            ]
            if drafts is not None:
                changes += [
                    PipelineWikiChange(field="body", before=None, after=t.content)
                    for t in drafts
                    if t.field is None
                ]
            metadata = PipelineWikiEdit(
                path=path,
                base_rev=state.base_rev,
                new_rev=state.edit.new_rev if state.edit else None,
                detected_at=_timestamp(state.detected_at),
                ready_at=_timestamp(state.ready_at),
                state="settling",
                changes=changes,
            )
            current = self.journal.receive_wiki_edit(
                PipelineItem(
                    schema_version=2,
                    revision=1,
                    capture_id=None,
                    source_id=source_id,
                    package_id=None,
                    package_revision=None,
                    source_type="wiki_edit",
                    title=Path(state.path).stem,
                    location="wiki",
                    status="working",
                    apply_failure_limit=self.journal.failure_limit,
                    stage_times={},
                    history=["wiki"],
                    threads=[],
                    pages=[],
                    digest=None,
                    updated_at=now,
                    wiki_edit=metadata,
                )
            )
            if drafts is None or any(t.thread_id not in threads for t in drafts):
                self.journal.update(
                    source_id, location="wiki", status="working", wiki_edit=metadata
                )
                continue
            if not drafts:
                self.journal.update(
                    source_id,
                    location="wiki",
                    status="done",
                    wiki_edit=metadata.model_copy(
                        update={"state": "unchanged", "ready_at": None}
                    ),
                )
                continue
            recorded = [threads[t.thread_id] for t in drafts]
            if current.wiki_edit is not None and current.wiki_edit.state == "published":
                # Recover the latest output hashes without replaying Fabric on every tick.
                pass
            elif current.location != "fabric":
                current = self.journal.update(
                    source_id,
                    location="fabric",
                    status="working",
                    threads=[
                        PipelineThread(
                            thread_id=t.thread_id, kind=t.kind, content=t.content or ""
                        )
                        for t in recorded
                        if t.kind != "실행"
                    ],
                    stage_times=current.stage_times | {"반영": now},
                    wiki_edit=metadata.model_copy(
                        update={"state": "recorded", "ready_at": None}
                    ),
                )
            pages = []
            digest = None
            source_ids = {t.source_id for t in recorded}
            for relative, data, stamp in published:
                if (
                    relative != state.path
                    and not any(t.thread_id.encode() in data for t in recorded)
                    and not any(sid.encode() in data for sid in source_ids)
                ):
                    continue
                full_path = f"{prefix}/{relative}" if prefix != "." else relative
                page = PipelinePage(
                    path=full_path,
                    updated_at=stamp,
                    sha256=hashlib.sha256(data).hexdigest(),
                )
                if relative.startswith("Digest/"):
                    if len(Path(relative).stem) == 10 and (
                        digest is None or stamp > digest.updated_at
                    ):
                        digest = page
                else:
                    pages.append(page)
            if self.compiler.bookmark() >= max(t.seq for t in recorded) and (
                pages or digest
            ):
                metadata = metadata.model_copy(
                    update={"state": "published", "ready_at": None}
                )
                stage_times = current.stage_times
                if "wiki갱신" not in stage_times:
                    stage_times = stage_times | {"wiki갱신": now}
                self.journal.update(
                    source_id,
                    location="wiki",
                    status="done",
                    wiki_edit=metadata,
                    pages=sorted(pages, key=lambda p: p.path),
                    digest=digest,
                    stage_times=stage_times,
                )
        # A candidate reverted to its baseline needs no Fabric record or review.
        for old in self.journal.wiki_edits():
            if (
                old.source_id not in active
                and old.wiki_edit is not None
                and old.wiki_edit.state == "settling"
            ):
                self.journal.update(
                    old.source_id,
                    status="done",
                    wiki_edit=old.wiki_edit.model_copy(
                        update={"state": "unchanged", "ready_at": None, "changes": []}
                    ),
                )
        self.journal.publish()
