"""Project promotion journal and append-only lifecycle side effects."""

import hashlib
import logging
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from weaver.context import ContextDictionary, DictionarySnapshot, _key
from weaver.context.proposals import LocationProposals, nearest_parent
from weaver.context.tree import UNCLASSIFIED, root_of
from weaver.fabric import Fabric, ThreadQuery
from weaver.fabric.state import project
from weaver.schema import TagCorrection, Thread, ThreadDraft
from weaver.wiki.generation import fingerprint

logger = logging.getLogger(__name__)
PROJECT_THREAD_PREFIX = "wiki-project:"


def _slug(name: str) -> str:
    """A project name as one path segment: spaces become dashes, separators go."""
    cleaned = "".join(
        ch for ch in unicodedata.normalize("NFC", name) if ch not in "/\\#^|[]"
    )
    slug = "-".join(cleaned.split())
    if slug:
        return slug
    return "project-" + hashlib.sha256(name.encode()).hexdigest()[:8]


class PromotionShape(BaseModel):
    """Provider-compatible nominal wire shape, without per-item semantic bounds."""

    model_config = ConfigDict(extra="forbid")
    name: str
    area: str
    goal: str
    end_state: str
    short_task: bool
    thread_ids: list[str]


class ProjectPromotion(PromotionShape):
    """A single independently validated semantic judgement from the wiki corpus."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    area: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=500)
    end_state: str = Field(min_length=1, max_length=500)
    short_task: bool
    thread_ids: list[str] = Field(min_length=2)

    @field_validator("name", "area", mode="before")
    @classmethod
    def canonical_unicode(cls, value: object) -> object:
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def distinct_evidence(self) -> "ProjectPromotion":
        if len(self.thread_ids) != len(set(self.thread_ids)):
            raise ValueError("duplicate evidence")
        return self


def active_records(threads: list[Thread]) -> list[Thread]:
    retracted = {t.retracts for t in threads if t.retracts}
    return [
        t
        for t in threads
        if t.thread_id not in retracted
        and not t.retracts
        and t.kind not in ("태그정정", "실행", "거절")
    ]


def activity(records: list[Thread]) -> datetime:
    """Source time, capped at recording time, never migration arrival time."""
    return max(min(t.occurred_at, t.recorded_at) for t in records)


class ProjectLifecycle:
    def __init__(
        self,
        path: Path,
        context: ContextDictionary,
        fabric: Fabric,
        timezone: ZoneInfo | None = None,
        default_sphere: str = "회사",
    ) -> None:
        self.default_sphere = default_sphere
        self.path, self.context, self.fabric, self.timezone = (
            path,
            context,
            fabric,
            timezone or ZoneInfo("UTC"),
        )
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS wiki_promotions "
                "(name TEXT PRIMARY KEY, payload TEXT NOT NULL, rejected INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS wiki_project_corrections "
                "(thread_id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS wiki_project_activity "
                "(name TEXT PRIMARY KEY, archive_seq INTEGER NOT NULL)"
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def _history(self) -> dict[str, tuple[ProjectPromotion, bool]]:
        with self._db() as db:
            return {
                name: (ProjectPromotion.model_validate_json(raw), bool(rejected))
                for name, raw, rejected in db.execute("SELECT * FROM wiki_promotions")
            }

    def _blocked(self, threads: list[Thread]) -> set[str]:
        """Only rename, retraction and tag removal veto this project's history."""
        dictionary = self.context.snapshot()
        with self._db() as db:
            corrections = dict(
                db.execute("SELECT thread_id, name FROM wiki_project_corrections")
            )
            blocked = set()
            for name, (candidate, rejected) in self._history().items():
                entry = dictionary.resolve(candidate.name)
                renamed = entry is not None and entry.name != candidate.name

                def removed(
                    label: str,
                    expected: str = _key(entry.name if entry else candidate.name),
                ) -> bool:
                    resolved = dictionary.resolve(label)
                    return _key(resolved.name if resolved else label) == expected

                reverted = any(
                    t.actor == "human"
                    and (
                        corrections.get(t.retracts or "") == name
                        or (
                            t.correction is not None
                            and t.correction.target in candidate.thread_ids
                            and any(removed(label) for label in t.correction.remove)
                        )
                    )
                    for t in threads
                )
                if rejected or renamed or reverted:
                    db.execute(
                        "UPDATE wiki_promotions SET rejected=1 WHERE name=?", (name,)
                    )
                    blocked.add(name)
                    if reverted and entry is not None and entry.kind == "project":
                        self.context.set_project_lifecycle(
                            entry.name,
                            promoted=False,
                            archived=entry.archived,
                            reverted=True,
                        )
        return blocked

    def _is_blocked(
        self, candidate: ProjectPromotion, threads: list[Thread], blocked: set[str]
    ) -> bool:
        name = _key(candidate.name)
        if name in blocked:
            return True
        history = self._history()
        # A different established project is independent even with shared evidence.
        if name in history:
            return False
        dictionary = self.context.snapshot()
        entry = dictionary.resolve(candidate.name)
        if (
            entry
            and entry.kind == "project"
            and entry.name == candidate.name
            and (entry.promoted or entry.human_edited)
            and not entry.promotion_reverted
        ):
            return False
        by_id = {t.thread_id: t for t in threads}
        sources = {by_id[key].source_id for key in candidate.thread_ids if key in by_id}
        area = dictionary.resolve(candidate.area)
        for key in blocked:
            old = history[key][0]
            old_area = dictionary.resolve(old.area)
            old_sources = {by_id[i].source_id for i in old.thread_ids if i in by_id}
            if (
                area
                and old_area
                and area.name == old_area.name
                and sources & old_sources
            ):
                return True
        return False

    def stable_threads(self, threads: list[Thread]) -> list[Thread]:
        """Remove only our own side effects from the generation cache identity."""
        additions: dict[str, set[str]] = {}
        with self._db() as db:
            for (raw,) in db.execute("SELECT payload FROM wiki_project_corrections"):
                draft = ThreadDraft.model_validate_json(raw)
                assert draft.correction is not None
                additions.setdefault(draft.correction.target, set()).update(
                    draft.correction.add
                )
        return [
            t.model_copy(
                update={
                    "tags": [
                        tag
                        for tag in t.tags
                        if tag not in additions.get(t.thread_id, set())
                    ]
                }
            )
            for t in threads
            if not t.thread_id.startswith(PROJECT_THREAD_PREFIX)
        ]

    def _canonical(
        self, threads: list[Thread], dictionary: DictionarySnapshot
    ) -> list[Thread]:
        return [
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

    def refresh(self, now: datetime) -> None:
        """Replay prepared payloads every tick and update archive state without LLMs."""
        threads = self.fabric.entries(ThreadQuery())
        blocked = self._blocked(threads)
        self._replay(threads, blocked, now)
        dictionary = self.context.snapshot()
        threads = self._canonical(self.fabric.entries(ThreadQuery()), dictionary)
        active = active_records(threads)
        history = self._history()
        for entry in dictionary.entries:
            if entry.kind != "project":
                continue
            records = [t for t in active if entry.name in t.tags]
            pending = history.get(_key(entry.name))
            if not records and entry.promoted and pending:
                # Registration may have succeeded before an interrupted append.
                # Its source evidence still ages while the journal is pending.
                records = [t for t in active if t.thread_id in pending[0].thread_ids]
            if not records:
                continue
            latest_seq = max(t.seq for t in records)
            with self._db() as db:
                row = db.execute(
                    "SELECT archive_seq FROM wiki_project_activity WHERE name=?",
                    (_key(entry.name),),
                ).fetchone()
            baseline = int(row[0]) if row else 0
            if entry.archived and latest_seq > baseline:
                self.context.set_project_lifecycle(entry.name, archived=False)
                with self._db() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO wiki_project_activity VALUES (?, ?)",
                        (_key(entry.name), latest_seq),
                    )
                continue
            if entry.archived:
                continue
            status = project(entry.name, threads).fields.get("status")
            status_seq = next(
                (t.seq for t in records if status and t.thread_id == status.thread_id),
                0,
            )
            complete = (
                status is not None
                and status.value in ("완료", "completed", "done")
                and status_seq > baseline
            )
            stale = entry.promoted and activity(records) <= now - timedelta(days=30)
            if complete or stale:
                self.context.set_project_lifecycle(entry.name, archived=True)
                with self._db() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO wiki_project_activity VALUES (?, ?)",
                        (_key(entry.name), latest_seq),
                    )

    def eligible(
        self,
        candidate: ProjectPromotion,
        active: list[Thread],
        now: datetime | None = None,
    ) -> bool:
        dictionary = self.context.snapshot()
        area = dictionary.resolve(candidate.area)
        entry = dictionary.resolve(candidate.name)
        by_id = {t.thread_id: t for t in active}
        if (
            candidate.short_task
            or not area
            or area.kind != "area"
            or not set(candidate.thread_ids).issubset(by_id)
            or (
                entry
                and (
                    entry.kind != "project"
                    or entry.name != candidate.name
                    or entry.archived
                    or entry.promotion_reverted
                )
            )
        ):
            return False
        selected = [by_id[key] for key in candidate.thread_ids]
        if any(
            {e.name for tag in t.tags if (e := dictionary.area_of(tag))} != {area.name}
            for t in selected
        ):
            return False
        if now is not None and activity(selected) <= now - timedelta(days=30):
            return False
        # Continuing an established project does not require rediscovering its
        # original multi-source/day cluster in every subset of new evidence.
        if entry and (entry.promoted or entry.human_edited):
            return True
        return (
            len({t.source_id for t in selected}) > 1
            or len({t.occurred_at.astimezone(self.timezone).date() for t in selected})
            > 1
        )

    def apply(
        self,
        candidates: list[JsonValue],
        active: list[Thread],
        now: datetime,
        *,
        replay: bool = True,
        proposals: LocationProposals | None = None,
    ) -> None:
        active = active_records(active)
        threads = self.fabric.entries(ThreadQuery())
        blocked = self._blocked(threads)
        seen: set[str] = set()
        for raw in candidates:
            try:
                candidate = ProjectPromotion.model_validate(raw)
            except ValidationError:
                logger.warning("wiki promotion skipped: invalid_item")
                continue
            name = _key(candidate.name)
            if name in seen:
                logger.warning("wiki promotion skipped: duplicate_name")
                continue
            seen.add(name)
            if name in blocked:
                logger.warning("wiki promotion skipped: reverted_name")
                continue
            dictionary = self.context.snapshot()
            selected = [t for t in active if t.thread_id in candidate.thread_ids]
            owners = {
                e.name
                for t in selected
                for tag in t.tags
                if (e := dictionary.resolve(tag))
                and e.kind == "project"
                and not e.archived
                and not e.promotion_reverted
            }
            if len(owners) > 1:
                logger.warning("wiki promotion skipped: multiple_projects")
                continue
            if owners:
                candidate = candidate.model_copy(update={"name": next(iter(owners))})
                name = _key(candidate.name)
            if self._is_blocked(candidate, threads, blocked) or not self.eligible(
                candidate, active, now
            ):
                logger.warning("wiki promotion skipped: ineligible")
                continue
            if proposals is not None:
                self._propose(proposals, candidate, active, now)
                continue
            with self._db() as db:
                previous = db.execute(
                    "SELECT payload FROM wiki_promotions WHERE name=?", (name,)
                ).fetchone()
                if previous:
                    old = ProjectPromotion.model_validate_json(previous[0])
                    old_area, new_area = (
                        dictionary.resolve(old.area),
                        dictionary.resolve(candidate.area),
                    )
                    if not old_area or not new_area or old_area.name != new_area.name:
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
                    if target.thread_id not in candidate.thread_ids or any(
                        (e := dictionary.resolve(tag)) and e.name == candidate.name
                        for tag in target.tags
                    ):
                        continue
                    key = PROJECT_THREAD_PREFIX + fingerprint(
                        {"name": name, "target": target.thread_id}
                    )
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
        if replay:
            self.refresh(now)

    def _propose(
        self,
        proposals: LocationProposals,
        candidate: ProjectPromotion,
        active: list[Thread],
        now: datetime,
    ) -> None:
        """Offer a promotion as a new project location; a person decides."""
        area = self.context.snapshot().area_of(candidate.area)
        root = (
            root_of(area.name)
            if area is not None and area.source == "tree"
            else self.default_sphere
        )
        by_id = {t.thread_id: t for t in active}
        selected = [by_id[key] for key in candidate.thread_ids if key in by_id]
        path = f"{root}/project/{_slug(candidate.name)}"
        proposals.propose(
            kind="location",
            reason="project_promotion",
            sphere=root,
            path=path,
            parent=nearest_parent(self.context.snapshot(), path),
            fallback=f"{root}/{UNCLASSIFIED}",
            thread_ids=[t.thread_id for t in selected],
            source_ids=sorted({t.source_id for t in selected}),
            now=now,
        )

    def _replay(self, threads: list[Thread], blocked: set[str], now: datetime) -> None:
        existing_ids = {t.thread_id for t in threads}
        active = active_records(threads)
        by_id = {t.thread_id: t for t in active}
        with self._db() as db:
            rows = db.execute(
                "SELECT name, payload FROM wiki_project_corrections"
            ).fetchall()
        history = self._history()
        for name, raw in rows:
            draft = ThreadDraft.model_validate_json(raw)
            if name in blocked or draft.thread_id in existing_ids:
                continue
            assert draft.correction is not None
            if draft.correction.target not in by_id:
                continue
            candidate = history[name][0]
            selected = candidate.model_copy(
                update={
                    "thread_ids": [key for key in candidate.thread_ids if key in by_id]
                }
            )
            if len(selected.thread_ids) < 2 or not self.eligible(selected, active, now):
                continue
            entry = self.context.add_entry(
                candidate.name,
                "project",
                human_edited=False,
                source_ids=tuple(
                    sorted({by_id[t].source_id for t in selected.thread_ids})
                ),
            )
            if (
                entry.name != candidate.name
                or entry.kind != "project"
                or entry.archived
                or entry.promotion_reverted
            ):
                continue
            self.context.set_project_lifecycle(
                entry.name,
                promoted=entry.promoted or not entry.human_edited,
                archived=False,
            )
            self.fabric.append([draft])
