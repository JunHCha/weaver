"""Build evidence-linked calendar digests from the Fabric log."""

import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, JsonValue

from weaver.context import DictionarySnapshot
from weaver.fabric.requests import request_values
from weaver.schema import Thread
from weaver.wiki import layout
from weaver.wiki.document import NOTICE, readable_links
from weaver.wiki.generation import GenerationCache, fingerprint
from weaver.wiki.links import link_label, wikilink
from weaver.wiki.prompts import BATCH_PROMPT, RULES_VERSION
from weaver.wiki.store import HumanEditPending, WikiStore, normalize_path

logger = logging.getLogger(__name__)


class Sentence(BaseModel):
    """One generated statement and its claimed source IDs."""

    model_config = ConfigDict(extra="forbid")
    text: str
    evidence: list[str]


class CardText(BaseModel):
    """A generated headline and current-state bullets for one card key."""

    model_config = ConfigDict(extra="forbid")
    key: str
    headline: str
    bullets: list[str] = []


class DigestEntry(BaseModel):
    """Generated sentences and card texts for one requested digest."""

    model_config = ConfigDict(extra="forbid")
    path: str
    sentences: list[Sentence]
    cards: list[CardText] = []


@dataclass
class DigestTarget:
    path: str
    period: str
    inputs: dict[str, object]
    evidence: list[str]
    previous: str
    top: str
    base: str | None
    fingerprint: str
    props: dict[str, JsonValue]
    tasks: str = ""
    day: str = ""
    card_keys: dict[str, list[str]] = field(default_factory=dict)

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "period": self.period,
            "inputs": self.inputs,
            "previous": self.previous,
            "evidence": self.evidence,
        }


def _day_path(day: date) -> str:
    return f"Digest/{day:%Y/%m/%Y-%m-%d}.md"


def _week_path(day: date) -> str:
    iso = day.isocalendar()
    return f"Digest/Weekly/{iso.year}-W{iso.week:02}.md"


def _month_path(day: date) -> str:
    return f"Digest/Monthly/{day:%Y-%m}.md"


_WEEKDAYS = "월화수목금토일"
_EXCLUDED = ("태그정정", "거절", "실행")
_SUMMARY_HEADING = "## 요약\n\n"
_TASKS_HEADING = "\n## 들어온 일\n\n"


def _top(day: date) -> str:
    return f"# {day.isoformat()} ({_WEEKDAYS[day.weekday()]})\n"


def card_texts(state_path: Path) -> dict[tuple[str, str], CardText]:
    """Read stored card texts by (day, key) without creating or locking state."""
    try:
        with closing(sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)) as db:
            rows = db.execute(
                "SELECT day, key, headline, bullets FROM cards"
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        (day, key): CardText(key=key, headline=headline, bullets=json.loads(bullets))
        for day, key, headline, bullets in rows
    }


def _threads_only(inputs: dict[str, object]) -> dict[str, object]:
    return {"threads": inputs["threads"]}


class DigestBuilder:
    """Plan and apply evidence-linked digests without making model calls."""

    def __init__(
        self,
        wiki: WikiStore,
        state_path: Path,
        timezone: ZoneInfo,
        source_link: Callable[[str], str | None],
        deferred: Callable[[HumanEditPending], None] | None = None,
        source_label: Callable[[str], str | None] | None = None,
        dictionary: Callable[[], DictionarySnapshot] | None = None,
    ) -> None:
        self.wiki, self.timezone, self.state_path = wiki, timezone, state_path
        self.dictionary = dictionary or (lambda: DictionarySnapshot([]))
        self.deferred = deferred
        self.pending = False

        def normalized_link(source_id: str) -> str | None:
            target = source_link(source_id)
            if target is None:
                return None
            label = source_label(source_id) if source_label else None
            return normalize_path(target) + (
                "|" + link_label(label) if label is not None else ""
            )

        self.source_link = normalized_link
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pages (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS bookmark (id INTEGER PRIMARY KEY CHECK (id=1), seq INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS summaries (path TEXT PRIMARY KEY, text TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS cards (day TEXT NOT NULL, key TEXT NOT NULL, "
                "headline TEXT NOT NULL, bullets TEXT NOT NULL, thread_ids TEXT NOT NULL, "
                "PRIMARY KEY (day, key))"
            )
        self.cache = GenerationCache(state_path)

    def _local_day(self, t: Thread) -> date:
        return t.occurred_at.astimezone(self.timezone).date()

    def _active(self, threads: list[Thread]) -> list[Thread]:
        retracted = {t.retracts for t in threads if t.retracts}
        return [
            t
            for t in threads
            if t.thread_id not in retracted
            and not t.retracts
            and t.kind not in _EXCLUDED
        ]

    def _task_blocks(self, threads: list[Thread]) -> dict[date, str]:
        """Checkbox lines per incoming day, rendered from the current state."""
        retracted = {t.retracts for t in threads if t.retracts}
        done: dict[str, date] = {}
        for t in sorted(threads, key=lambda t: t.seq):
            if t.resolves and not t.retracts and t.thread_id not in retracted:
                done.setdefault(t.resolves, self._local_day(t))
        deadlines, _ = request_values(threads)
        lines: dict[date, list[str]] = {}
        for t in self._active(threads):
            if t.kind != "요청":
                continue
            raw = deadlines.get(t.thread_id)
            try:
                due = date.fromisoformat(raw[:10]) if raw else None
            except ValueError:
                due = None
            lines.setdefault(self._local_day(t), []).append(
                layout.task_line(
                    t.content or "",
                    layout.block_id(t.thread_id),
                    deadline=due,
                    done=done.get(t.thread_id),
                )
            )
        return {
            day: _TASKS_HEADING + "\n".join(rows) + "\n" for day, rows in lines.items()
        }

    def plan(self, threads: list[Thread], high: int) -> list[DigestTarget]:
        """Plan changed periods from a bounded canonical Fabric snapshot."""
        self.pending = False
        threads = [t for t in threads if t.seq <= high]
        active = self._active(threads)
        # Only the day an item came in is a Digest day; deadlines never are.
        days = {self._local_day(t) for t in threads if t.kind not in _EXCLUDED}
        tasks = self._task_blocks(threads)
        dictionary = self.dictionary()
        daily: dict[date, dict[str, object]] = {}
        day_evidence: dict[date, list[str]] = {}
        targets: list[DigestTarget] = []
        for day in sorted(days):
            items = [t for t in active if self._local_day(t) == day]
            grouped: dict[str, list[Thread]] = {}
            for t in items:
                grouped.setdefault(layout.case_key(t, dictionary), []).append(t)
            cards: list[dict[str, Any]] = [
                {
                    "key": key,
                    "thread_ids": [t.thread_id for t in group],
                    "contents": [t.content for t in group],
                }
                for key, group in sorted(grouped.items())
            ]
            evidence = sorted({t.source_id for t in items})
            inputs: dict[str, object] = {
                "threads": [t.model_dump(mode="json") for t in items],
                "cards": cards,
            }
            daily[day], day_evidence[day] = inputs, evidence
            self._plan_target(
                targets,
                _day_path(day),
                "daily",
                inputs,
                evidence,
                _top(day),
                tasks=tasks.get(day, ""),
                day=day.isoformat(),
                card_keys={str(c["key"]): list(c["thread_ids"]) for c in cards},
            )
        weeks = sorted({day - timedelta(days=day.weekday()) for day in days})
        for week in weeks:
            members = sorted(d for d in days if week <= d < week + timedelta(days=7))
            inputs = {"days": {d.isoformat(): _threads_only(daily[d]) for d in members}}
            evidence = sorted({source for d in members for source in day_evidence[d]})
            self._plan_target(targets, _week_path(week), "weekly", inputs, evidence, "")
        for month in sorted({day.replace(day=1) for day in days}):
            # A month contains all the days in its overlapping ISO weeks.
            month_weeks = {
                day - timedelta(days=day.weekday())
                for day in days
                if day.year == month.year and day.month == month.month
            }
            members = sorted(
                day
                for day in days
                if day - timedelta(days=day.weekday()) in month_weeks
            )
            inputs = {"days": {d.isoformat(): _threads_only(daily[d]) for d in members}}
            evidence = sorted({source for d in members for source in day_evidence[d]})
            self._plan_target(
                targets, _month_path(month), "monthly", inputs, evidence, ""
            )
        self._retire_stale(
            {_day_path(d) for d in days}
            | {_week_path(d) for d in days}
            | {_month_path(d) for d in days}
        )
        return targets

    def _retire_stale(self, planned: set[str]) -> None:
        """Remove untouched Digest files Weaver wrote for days no longer planned."""
        with sqlite3.connect(self.state_path) as db:
            known = [
                p
                for (p,) in db.execute("SELECT path FROM pages")
                if p.startswith("Digest/") and p not in planned
            ]
        for path in known:
            try:
                _, _, base = self.wiki.read(path)
            except FileNotFoundError:
                base = None
            if base is not None:
                if not self.wiki.is_tracked_version(path, base):
                    continue
                try:
                    self.wiki.retire_summary(path, base)
                except (FileNotFoundError, HumanEditPending):
                    continue
            with sqlite3.connect(self.state_path) as db:
                db.execute("DELETE FROM pages WHERE path=?", (path,))
                db.execute("DELETE FROM summaries WHERE path=?", (path,))
                db.execute("DELETE FROM cards WHERE day=?", (path[-13:-3],))

    def _plan_target(
        self,
        targets: list[DigestTarget],
        path: str,
        period: str,
        inputs: dict[str, object],
        evidence: list[str],
        top: str,
        *,
        tasks: str = "",
        day: str = "",
        card_keys: dict[str, list[str]] | None = None,
    ) -> None:
        key = fingerprint(
            {"inputs": inputs, "rules": RULES_VERSION, "prompt": BATCH_PROMPT}
        )
        with sqlite3.connect(self.state_path) as db:
            row = db.execute(
                "SELECT fingerprint FROM pages WHERE path=?", (path,)
            ).fetchone()
        try:
            props, previous, base = self.wiki.read(path)
            if row and row[0] == key:
                return
        except FileNotFoundError:
            props, previous, base = {}, "", None
        try:
            self.wiki.check_write(path, base)
        except HumanEditPending as error:
            self.pending = True
            if self.deferred:
                self.deferred(error)
            return
        targets.append(
            DigestTarget(
                path,
                period,
                inputs,
                evidence,
                previous,
                top,
                base,
                key,
                props,
                tasks,
                day,
                card_keys or {},
            )
        )

    def _valid_cards(self, target: DigestTarget, entry: DigestEntry) -> list[CardText]:
        cards: dict[str, CardText] = {}
        for card in entry.cards:
            headline = " ".join(card.headline.split())
            if card.key not in target.card_keys or card.key in cards:
                logger.warning("digest card rejected: unknown or duplicate key")
                continue
            if not headline:
                logger.warning("digest card rejected: empty headline")
                continue
            bullets = [" ".join(b.split()) for b in card.bullets]
            bullets = [b[:80] for b in bullets if b][:2]
            if (
                len(headline) > 40
                or len(card.bullets) > 2
                or any(len(b) > 80 for b in card.bullets)
            ):
                logger.warning("digest card clipped to length limits")
            cards[card.key] = CardText(
                key=card.key, headline=headline[:40], bullets=bullets
            )
        return list(cards.values())

    def _store_cards(self, target: DigestTarget, cards: list[CardText]) -> None:
        with sqlite3.connect(self.state_path) as db:
            db.execute("DELETE FROM cards WHERE day=?", (target.day,))
            db.executemany(
                "INSERT INTO cards VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        target.day,
                        c.key,
                        c.headline,
                        json.dumps(c.bullets, ensure_ascii=False),
                        json.dumps(target.card_keys[c.key], ensure_ascii=False),
                    )
                    for c in cards
                ],
            )

    def apply(
        self, targets: list[DigestTarget], entries: list[DigestEntry], high: int
    ) -> None:
        """Write present valid sentences independently; omitted periods remain stale."""
        by_path: dict[str, DigestEntry] = {}
        requested = {t.path for t in targets}
        for entry in entries:
            if entry.path not in requested or entry.path in by_path:
                logger.warning("digest entry rejected: unknown or duplicate path")
                continue
            by_path[entry.path] = entry
        for target in targets:
            found = by_path.get(target.path)
            if found is None:
                logger.warning("digest entry missing")
                continue
            lines = []
            for sentence in found.sentences:
                if (
                    not sentence.text.strip()
                    or "\n" in sentence.text
                    or "\r" in sentence.text
                    or not sentence.evidence
                    or any(item not in target.evidence for item in sentence.evidence)
                ):
                    logger.warning("digest sentence rejected: invalid evidence or text")
                    continue
                links = " ".join(
                    wikilink(self.source_link(item))
                    for item in dict.fromkeys(sentence.evidence)
                )
                lines.append(f"- {sentence.text} {links}")
            heading = _SUMMARY_HEADING if target.period == "daily" else ""
            summary = heading + "\n".join(lines) + "\n"
            cache_inputs = {
                "payload": target.payload(),
                "base": target.base,
                "top": target.top,
                "links": [self.source_link(item) for item in target.evidence],
            }
            summary = self.cache.get(target.path, cache_inputs, partial(str, summary))
            cards = self._valid_cards(target, found)
            target.props["source_ids"] = list[JsonValue](target.evidence)
            try:
                self.wiki.write(
                    target.path,
                    target.props,
                    target.top + target.tasks + ("\n" if target.top else "") + summary,
                    target.base,
                    metadata="digest update",
                    readonly=True,
                )
            except HumanEditPending as error:
                self.pending = True
                if self.deferred:
                    self.deferred(error)
                continue
            with sqlite3.connect(self.state_path) as db:
                db.execute(
                    "INSERT OR REPLACE INTO pages VALUES (?, ?)",
                    (target.path, target.fingerprint),
                )
                db.execute(
                    "INSERT OR REPLACE INTO summaries VALUES (?, ?)",
                    (target.path, summary),
                )
            if target.period == "daily":
                self._store_cards(target, cards)
            self.cache.discard(target.path)
        with sqlite3.connect(self.state_path) as db:
            db.execute("INSERT OR REPLACE INTO bookmark VALUES (1, ?)", (high,))

    def refresh_tasks(self, rows: list[Thread], high: int) -> list[str]:
        """Rewrite only the checkbox lines of existing daily files; no LLM call."""
        self.pending = False
        threads = [t for t in rows if t.seq <= high]
        tasks = self._task_blocks(threads)
        days = {self._local_day(t) for t in threads if t.kind not in _EXCLUDED}
        changed: list[str] = []
        for day in sorted(days):
            path = _day_path(day)
            with sqlite3.connect(self.state_path) as db:
                row = db.execute(
                    "SELECT text FROM summaries WHERE path=?", (path,)
                ).fetchone()
            if row is None:
                continue
            try:
                props, body, base = self.wiki.read(path)
            except FileNotFoundError:
                continue
            if not self.wiki.is_tracked_version(path, base):
                # A person edited the file (e.g. ticked a box); leave it alone.
                self.pending = True
                if self.deferred:
                    self.deferred(HumanEditPending(path))
                continue
            new = _top(day) + tasks.get(day, "") + "\n" + row[0]
            if body.rstrip("\n") == readable_links(NOTICE + new).rstrip("\n"):
                continue
            try:
                self.wiki.write(
                    path, props, new, base, metadata="digest tasks", readonly=True
                )
            except HumanEditPending as error:
                self.pending = True
                if self.deferred:
                    self.deferred(error)
                continue
            changed.append(path)
        return changed
