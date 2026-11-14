"""Build evidence-linked calendar digests from the Fabric log."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, JsonValue

from weaver.fabric import ThreadQuery
from weaver.fabric.store import Fabric
from weaver.llm.types import LlmClient
from weaver.schema import Thread
from weaver.wiki.generation import GenerationCache
from weaver.wiki.links import wikilink
from weaver.wiki.prompts import DIGEST_PROMPT, RULES_VERSION
from weaver.wiki.snapshot import canonical_threads
from weaver.wiki.store import WikiStore, normalize_path


class Sentence(BaseModel):
    """One generated statement and the source IDs supporting it."""

    text: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class Summary(BaseModel):
    """Structured response used to attach verified source links."""

    sentences: list[Sentence]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


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
    """Incrementally build digests; source_link maps source IDs to vault note paths.

    State is private to this consumer. An exception leaves the bookmark unchanged;
    completed page fingerprints let retries resume without rewriting those pages.
    A single builder must run at a time for a given state database.
    """

    def __init__(
        self,
        fabric: Fabric,
        wiki: WikiStore,
        llm: LlmClient,
        state_path: Path,
        timezone: ZoneInfo,
        source_link: Callable[[str], str | None],
    ) -> None:
        self.fabric, self.wiki, self.llm = fabric, wiki, llm
        self.timezone = timezone

        def normalized_link(source_id: str) -> str | None:
            target = source_link(source_id)
            return normalize_path(target) if target is not None else None

        self.source_link = normalized_link
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = state_path
        with sqlite3.connect(state_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pages (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS bookmark (id INTEGER PRIMARY KEY CHECK (id=1), seq INTEGER NOT NULL)"
            )

        self.cache = GenerationCache(state_path)

    def _write(
        self, path: str, inputs: object, evidence: list[str], top: str = ""
    ) -> bool:
        path = normalize_path(path)
        fingerprint = _digest(
            {"inputs": inputs, "rules": RULES_VERSION, "prompt": DIGEST_PROMPT}
        )
        with sqlite3.connect(self.state_path) as db:
            row = db.execute(
                "SELECT fingerprint FROM pages WHERE path=?", (path,)
            ).fetchone()
        try:
            props, previous, base = self.wiki.read(path)
            if row and row[0] == fingerprint:
                return False
        except FileNotFoundError:
            props, previous, base = {}, "", None
        self.wiki.check_write(path, base)
        period = (
            "weekly"
            if "/Weekly/" in path
            else "monthly"
            if "/Monthly/" in path
            else "daily"
        )
        payload = {
            "inputs": inputs,
            "previous": previous,
            "evidence": evidence,
            "period": period,
            "path": path,
        }
        body = self.cache.get(
            path,
            {
                "payload": payload,
                "base": base,
                "top": top,
                "links": [self.source_link(item) for item in evidence],
            },
            lambda: self._generate(payload, evidence, top),
        )
        props["source_ids"] = list[JsonValue](evidence)
        self.wiki.write(path, props, body, base, metadata="digest update")
        with sqlite3.connect(self.state_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO pages VALUES (?, ?)", (path, fingerprint)
            )
        self.cache.discard(path)
        return True

    def _generate(
        self, payload: dict[str, object], evidence: list[str], top: str
    ) -> str:
        result = (
            self.llm.generate_json(
                "wiki.digest",
                DIGEST_PROMPT,
                json.dumps(payload, ensure_ascii=False),
                Summary,
            )
            if evidence
            else Summary(sentences=[])
        )
        lines = []
        for sentence in result.sentences:
            if (
                "\n" in sentence.text
                or "\r" in sentence.text
                or not sentence.evidence
                or any(item not in evidence for item in sentence.evidence)
            ):
                raise ValueError("digest sentence has invalid evidence")
            links = " ".join(
                wikilink(self.source_link(item))
                for item in dict.fromkeys(sentence.evidence)
            )
            lines.append(f"- {sentence.text} {links}")
        return top + "\n".join(lines) + "\n"

    def update(self) -> None:
        """Consume new Threads and refresh changed days, weeks, and months."""
        with sqlite3.connect(self.state_path) as db:
            row = db.execute("SELECT seq FROM bookmark WHERE id=1").fetchone()
        bookmark = row[0] if row else 0
        new = self.fabric.entries(ThreadQuery(after_seq=bookmark))
        if not new:
            return
        threads = canonical_threads(self.fabric.entries(ThreadQuery()))
        # Bound the snapshot to the batch, so a concurrent append is consumed next time.
        high = max(t.seq for t in new)
        threads = [t for t in threads if t.seq <= high]
        retracted = {t.retracts for t in threads if t.retracts}
        active = [
            t
            for t in threads
            if t.thread_id not in retracted
            and not t.retracts
            and t.kind not in ("태그정정", "거절")
        ]
        days = sorted(
            {
                t.occurred_at.astimezone(self.timezone).date()
                for t in threads
                if t.kind not in ("태그정정", "거절")
            }
        )
        by_day = {
            day: [
                t
                for t in active
                if t.occurred_at.astimezone(self.timezone).date() == day
            ]
            for day in days
        }
        # Last field value per tag is the Fabric projection, even when supplied on another day.
        fields: dict[tuple[str, str], Thread] = {}
        for t in active:
            if t.field in ("deadline", "schedule"):
                for tag in t.tags:
                    fields[tag, t.field] = t
        # Include dates named by fields, including superseded dates that may need clearing.
        days = sorted(
            set(days)
            | {
                value_day
                for t in threads
                if t.field in ("deadline", "schedule")
                for value_day in [_value_day(t.value, self.timezone)]
                if value_day is not None
            }
        )
        by_day = {day: by_day.get(day, []) for day in days}
        day_evidence: dict[date, list[str]] = {}
        for day in days:
            items = by_day[day]
            due = sorted(
                {
                    t.thread_id: t
                    for t in fields.values()
                    if _value_day(t.value, self.timezone) == day
                }.values(),
                key=lambda t: t.seq,
            )
            requests = [t for t in items if t.kind == "요청"]
            top_lines = ["## 마감·일정과 새 요청"]
            for t in {t.thread_id: t for t in [*due, *requests]}.values():
                top_lines.append(
                    f"- {t.field or '요청'}: {t.content} ({t.value if t.field else t.direction}) {wikilink(self.source_link(t.source_id))}"
                )
            top = "\n".join(top_lines) + "\n\n## 요약\n"
            evidence = sorted({t.source_id for t in [*items, *due]})
            day_evidence[day] = evidence
            inputs = {"threads": [t.model_dump(mode="json") for t in items], "top": top}
            self._write(_day_path(day), inputs, evidence, top)
        weeks = sorted({day - timedelta(days=day.weekday()) for day in days})
        for week in weeks:
            members = [d for d in days if week <= d < week + timedelta(days=7)]
            if not members:
                continue
            pages = {_day_path(d): self.wiki.read(_day_path(d))[1] for d in members}
            evidence = sorted({source for d in members for source in day_evidence[d]})
            with sqlite3.connect(self.state_path) as db:
                versions = {
                    p: db.execute(
                        "SELECT fingerprint FROM pages WHERE path=?", (p,)
                    ).fetchone()[0]
                    for p in pages
                }
            self._write(
                _week_path(week), {"pages": pages, "versions": versions}, evidence
            )
        months = sorted({day.replace(day=1) for day in days})
        for month in months:
            members = [
                w
                for w in weeks
                if any(
                    d.year == month.year
                    and d.month == month.month
                    and w <= d < w + timedelta(days=7)
                    for d in days
                )
            ]
            if not members:
                continue
            pages = {_week_path(w): self.wiki.read(_week_path(w))[1] for w in members}
            with sqlite3.connect(self.state_path) as db:
                versions = {
                    p: db.execute(
                        "SELECT fingerprint FROM pages WHERE path=?", (p,)
                    ).fetchone()[0]
                    for p in pages
                }
            evidence = sorted(
                {
                    source
                    for w in members
                    for d in days
                    if w <= d < w + timedelta(days=7)
                    for source in day_evidence[d]
                }
            )
            self._write(
                _month_path(month), {"pages": pages, "versions": versions}, evidence
            )
        with sqlite3.connect(self.state_path) as db:
            db.execute("INSERT OR REPLACE INTO bookmark VALUES (1, ?)", (high,))
