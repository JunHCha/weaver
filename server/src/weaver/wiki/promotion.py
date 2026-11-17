"""Project promotion journal and append-only lifecycle side effects."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from weaver.context import ContextDictionary, _key
from weaver.fabric import Fabric, ThreadQuery
from weaver.fabric.state import project
from weaver.schema import TagCorrection, Thread, ThreadDraft
from weaver.wiki.generation import fingerprint


class ProjectPromotion(BaseModel):
    """Semantic judgement from the complete wiki corpus, with bounded evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    area: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=500)
    end_state: str = Field(min_length=1, max_length=500)
    short_task: bool
    thread_ids: list[str] = Field(min_length=2)


class ProjectLifecycle:
    def __init__(self, path: Path, context: ContextDictionary, fabric: Fabric) -> None:
        self.path, self.context, self.fabric = path, context, fabric
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS wiki_promotions "
                "(name TEXT PRIMARY KEY, payload TEXT NOT NULL, rejected INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS wiki_project_corrections "
                "(thread_id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    def _blocked(self, threads: list[Thread]) -> tuple[set[str], set[str]]:
        """Human overrides are durable even if later records change the cluster name."""
        with sqlite3.connect(self.path) as db:
            corrections = dict(
                db.execute("SELECT thread_id, name FROM wiki_project_corrections")
            )
            blocked = set()
            evidence = set()
            for name, raw, rejected in db.execute(
                "SELECT * FROM wiki_promotions"
            ).fetchall():
                candidate = ProjectPromotion.model_validate_json(raw)
                entry = self.context.resolve(candidate.name)
                override = entry and (
                    entry.name != candidate.name or entry.human_edited
                )
                reverted = any(
                    t.actor == "human"
                    and (
                        corrections.get(t.retracts or "") == name
                        or (
                            t.correction
                            and _key(candidate.name) in map(_key, t.correction.remove)
                            and t.correction.target in candidate.thread_ids
                        )
                    )
                    for t in threads
                )
                if rejected or override or reverted:
                    db.execute(
                        "UPDATE wiki_promotions SET rejected=1 WHERE name=?", (name,)
                    )
                    blocked.add(name)
                    evidence.update(candidate.thread_ids)
        return blocked, evidence

    def refresh(self, now: datetime) -> None:
        """Check human overrides and archive even when no new Fabric row arrives."""
        threads = self.fabric.entries(ThreadQuery())
        self._blocked(threads)
        dictionary = self.context.snapshot()
        threads = [
            t.model_copy(
                update={
                    "tags": list(
                        dict.fromkeys(
                            e.name if (e := dictionary.resolve(tag)) else tag
                            for tag in t.tags
                        )
                    )
                }
            )
            for t in threads
        ]
        retracted = {t.retracts for t in threads if t.retracts}
        for entry in dictionary.entries:
            if entry.kind != "project" or entry.archived:
                continue
            records = [
                t
                for t in threads
                if entry.name in t.tags
                and t.thread_id not in retracted
                and not t.retracts
                and t.kind not in ("태그정정", "실행", "거절")
            ]
            status = project(entry.name, threads).fields.get("status")
            complete = status is not None and status.value in (
                "완료",
                "completed",
                "done",
            )
            stale = bool(records) and max(
                t.recorded_at for t in records
            ) <= now - timedelta(days=30)
            if complete or stale:
                self.context.set_project_lifecycle(entry.name, archived=True)

    def eligible(self, candidate: ProjectPromotion, active: list[Thread]) -> bool:
        dictionary = self.context.snapshot()
        area = dictionary.resolve(candidate.area)
        entry = dictionary.resolve(candidate.name)
        by_id = {t.thread_id: t for t in active}
        if (
            candidate.short_task
            or not area
            or area.kind != "area"
            or len(set(candidate.thread_ids)) < 2
            or not set(candidate.thread_ids).issubset(by_id)
            or (
                entry
                and (
                    entry.kind != "project"
                    or entry.human_edited
                    or entry.name != candidate.name
                    or entry.archived
                )
            )
        ):
            return False
        selected = [by_id[key] for key in set(candidate.thread_ids)]
        if any(
            {
                e.name
                for tag in t.tags
                if (e := dictionary.resolve(tag)) and e.kind == "area"
            }
            != {area.name}
            for t in selected
        ):
            return False
        return (
            len({t.source_id for t in selected}) > 1
            or len({t.occurred_at.date() for t in selected}) > 1
        )

    def apply(
        self, candidates: list[ProjectPromotion], active: list[Thread], now: datetime
    ) -> None:
        threads = self.fabric.entries(ThreadQuery())
        blocked, evidence = self._blocked(threads)
        existing_ids = {t.thread_id for t in threads}
        for candidate in candidates:
            name = _key(candidate.name)
            if (
                name in blocked
                or evidence.intersection(candidate.thread_ids)
                or not self.eligible(candidate, active)
            ):
                continue
            with sqlite3.connect(self.path) as db:
                previous = db.execute(
                    "SELECT payload FROM wiki_promotions WHERE name=?", (name,)
                ).fetchone()
                if previous:
                    old = ProjectPromotion.model_validate_json(previous[0])
                    if _key(old.area) != _key(candidate.area):
                        continue
                    candidate = candidate.model_copy(
                        update={
                            "thread_ids": sorted(
                                set(old.thread_ids + candidate.thread_ids)
                            )
                        }
                    )
                db.execute(
                    "INSERT INTO wiki_promotions VALUES (?, ?, 0) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                    (name, candidate.model_dump_json()),
                )
                for target in active:
                    if (
                        target.thread_id not in candidate.thread_ids
                        or candidate.name in target.tags
                    ):
                        continue
                    key = "wiki-project:" + fingerprint(
                        {"name": name, "target": target.thread_id}
                    )
                    if key in existing_ids:
                        continue
                    draft = ThreadDraft(
                        thread_id=key,
                        kind="태그정정",
                        content="Wiki 프로젝트 승격",
                        tags=[],
                        source_id=target.source_id,
                        actor="system",
                        occurred_at=now,
                        recorded_at=now,
                        field=None,
                        value=None,
                        direction=None,
                        resolves=None,
                        retracts=None,
                        correction=TagCorrection(
                            target=target.thread_id, add=[candidate.name], remove=[]
                        ),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO wiki_project_corrections VALUES (?, ?, ?)",
                        (key, name, draft.model_dump_json()),
                    )
        # Replay exact prepared payloads, including timestamps, after interruptions.
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT name, payload FROM wiki_project_corrections"
            ).fetchall()
        active_ids = {t.thread_id for t in active}
        for name, payload in rows:
            if name in blocked:
                continue
            draft = ThreadDraft.model_validate_json(payload)
            if draft.thread_id in existing_ids:
                continue
            assert draft.correction is not None
            if draft.correction.target not in active_ids:
                continue
            entry = self.context.add_entry(
                draft.correction.add[0], "project", human_edited=False
            )
            if (
                entry.human_edited
                or entry.name != draft.correction.add[0]
                or entry.kind != "project"
                or entry.archived
            ):
                continue
            self.context.set_project_lifecycle(
                entry.name, promoted=True, archived=entry.archived
            )
            self.fabric.append([draft])
        self.refresh(now)
