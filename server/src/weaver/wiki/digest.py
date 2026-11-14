"""Build evidence-linked calendar digests from the Fabric log."""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, JsonValue

from weaver.schema import Thread
from weaver.wiki.generation import GenerationCache, fingerprint
from weaver.wiki.links import wikilink
from weaver.wiki.prompts import BATCH_PROMPT, RULES_VERSION
from weaver.wiki.store import HumanEditPending, WikiStore, normalize_path

logger = logging.getLogger(__name__)


class Sentence(BaseModel):
    """One generated statement and its claimed source IDs."""

    model_config = ConfigDict(extra="forbid")
    text: str
    evidence: list[str]


class DigestEntry(BaseModel):
    """Generated sentences for one requested digest."""

    model_config = ConfigDict(extra="forbid")
    path: str
    sentences: list[Sentence]


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


def _value_day(value: object, zone: ZoneInfo) -> date | None:
    if isinstance(value, dict):
        value = value.get("start")
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed.astimezone(zone).date() if parsed.utcoffset() is not None else None
        )
    except ValueError:
        return None


class DigestBuilder:
    """Plan and apply evidence-linked digests without making model calls."""

    def __init__(
        self,
        wiki: WikiStore,
        state_path: Path,
        timezone: ZoneInfo,
        source_link: Callable[[str], str | None],
        deferred: Callable[[HumanEditPending], None] | None = None,
    ) -> None:
        self.wiki, self.timezone, self.state_path = wiki, timezone, state_path
        self.deferred = deferred
        self.pending = False

        def normalized_link(source_id: str) -> str | None:
            target = source_link(source_id)
            return normalize_path(target) if target is not None else None

        self.source_link = normalized_link
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pages (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS bookmark (id INTEGER PRIMARY KEY CHECK (id=1), seq INTEGER NOT NULL)"
            )
        self.cache = GenerationCache(state_path)

    def plan(self, threads: list[Thread], high: int) -> list[DigestTarget]:
        """Plan changed periods from a bounded canonical Fabric snapshot."""
        self.pending = False
        threads = [t for t in threads if t.seq <= high]
        retracted = {t.retracts for t in threads if t.retracts}
        active = [
            t
            for t in threads
            if t.thread_id not in retracted
            and not t.retracts
            and t.kind not in ("태그정정", "거절")
        ]
        days = {
            t.occurred_at.astimezone(self.timezone).date()
            for t in threads
            if t.kind not in ("태그정정", "거절")
        }
        days.update(
            day
            for t in threads
            if t.field in ("deadline", "schedule")
            for day in [_value_day(t.value, self.timezone)]
            if day is not None
        )
        fields: dict[tuple[str, str], Thread] = {}
        for t in active:
            if t.field in ("deadline", "schedule"):
                for tag in t.tags:
                    fields[tag, t.field] = t
        daily: dict[date, dict[str, object]] = {}
        day_evidence: dict[date, list[str]] = {}
        targets: list[DigestTarget] = []
        for day in sorted(days):
            items = [
                t
                for t in active
                if t.occurred_at.astimezone(self.timezone).date() == day
            ]
            due = sorted(
                {
                    t.thread_id: t
                    for t in fields.values()
                    if _value_day(t.value, self.timezone) == day
                }.values(),
                key=lambda t: t.seq,
            )
            requests = [t for t in items if t.kind == "요청"]
            lines = ["## 마감·일정과 새 요청"]
            for t in {t.thread_id: t for t in [*due, *requests]}.values():
                lines.append(
                    f"- {t.field or '요청'}: {t.content} ({t.value if t.field else t.direction}) {wikilink(self.source_link(t.source_id))}"
                )
            top = "\n".join(lines) + "\n\n## 요약\n"
            evidence = sorted({t.source_id for t in [*items, *due]})
            inputs: dict[str, object] = {
                "threads": [t.model_dump(mode="json") for t in items],
                "due": [t.model_dump(mode="json") for t in due],
                "top": top,
            }
            daily[day], day_evidence[day] = inputs, evidence
            self._plan_target(targets, _day_path(day), "daily", inputs, evidence, top)
        weeks = sorted({day - timedelta(days=day.weekday()) for day in days})
        for week in weeks:
            members = sorted(d for d in days if week <= d < week + timedelta(days=7))
            inputs = {"days": {d.isoformat(): daily[d] for d in members}}
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
            inputs = {"days": {d.isoformat(): daily[d] for d in members}}
            evidence = sorted({source for d in members for source in day_evidence[d]})
            self._plan_target(
                targets, _month_path(month), "monthly", inputs, evidence, ""
            )
        return targets

    def _plan_target(
        self,
        targets: list[DigestTarget],
        path: str,
        period: str,
        inputs: dict[str, object],
        evidence: list[str],
        top: str,
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
                path, period, inputs, evidence, previous, top, base, key, props
            )
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
            body = target.top + "\n".join(lines) + "\n"
            cache_inputs = {
                "payload": target.payload(),
                "base": target.base,
                "top": target.top,
                "links": [self.source_link(item) for item in target.evidence],
            }
            body = self.cache.get(target.path, cache_inputs, partial(str, body))
            target.props["source_ids"] = list[JsonValue](target.evidence)
            try:
                self.wiki.write(
                    target.path,
                    target.props,
                    body,
                    target.base,
                    metadata="digest update",
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
            self.cache.discard(target.path)
        with sqlite3.connect(self.state_path) as db:
            db.execute("INSERT OR REPLACE INTO bookmark VALUES (1, ?)", (high,))
