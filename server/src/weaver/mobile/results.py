"""Result read model: cards, tasks, documents, evidence and the source log.

Everything is derived on request. Cards group the facts that came in on one day (or fall due on one day) by case
key. Text comes from the Digest's stored card texts, placement from
``weaver.wiki.layout`` and links from the compiled documents' per-bullet
evidence. Everything is opened read-only; nothing here writes or calls a model.
"""

import hashlib
import re
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from weaver.archive import SourceNotFound
from weaver.context import DictionarySnapshot
from weaver.context.tree import node_kind, root_of
from weaver.reference import find_references
from weaver.runtime.summary import SummaryLookup
from weaver.schema.models import (
    DocBullet,
    DocSection,
    DocumentSummary,
    DocumentView,
    Evidence,
    EvidenceSource,
    EvidenceThread,
    LinkTarget,
    RefLinkView,
    ResultCard,
    SourceLogEntry,
    TaskItem,
    TreeNodeView,
    UsedIn,
)
from weaver.wiki import layout
from weaver.wiki.digest import CardText, _day_path, card_texts
from weaver.wiki.document import NOTICE
from weaver.wiki.store import WikiStore, _hash

from .errors import ApiError
from .read import MobileThread, ReadModel

Window = Literal["today", "around"]
DueKind = Literal["deadline", "schedule"]
AROUND_DAYS = 7
TREE_DAYS = 7
SOURCE_TEXT_LIMIT = 20000
DOC_REFERENCES = 5
type DocKind = Literal["area", "project", "topic", "digest"]
MAX_COUNTERPARTS = 3
type Meta = dict[str, dict[str, JsonValue]]
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CAUTION = re.compile(
    r"^- \*\*(?P<month>\d{1,2})/(?P<day>\d{1,2})\*\* \[(?P<label>[^\]]+)\] (?P<text>.+)$"
)
_CURSOR = re.compile(r"o[0-9]{1,9}")
_WIKILINK = re.compile(r"\s*\[\[[^\]\n]+\]\]")
_WIKI_ERRORS = (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError)


@dataclass(frozen=True)
class _Due:
    day: date
    kind: DueKind


class ResultModel:
    def __init__(self, read: ReadModel) -> None:
        self.read = read

    # ---- shared helpers ----------------------------------------------------------

    def _moment(self, at: datetime | None) -> datetime:
        zone = self.read.config.time.zone
        return (at or self.read.clock()).astimezone(zone)

    def _dictionary(self) -> DictionarySnapshot:
        return self.read.snapshot().dictionary or DictionarySnapshot([])

    def _compiled_seq(self) -> int:
        """The older of the two compile bookmarks, read without creating state."""
        root = self.read.config.state_root
        return min(
            _bookmark(root / "wiki-documents.sqlite3"),
            _bookmark(root / "wiki-digest.sqlite3"),
        )

    def _incoming(self, t: MobileThread) -> date:
        return t.occurred_at.astimezone(self.read.config.time.zone).date()

    def _due(self, t: MobileThread) -> _Due | None:
        if t.kind == "요청" and t.open and t.effective_deadline:
            try:
                return _Due(date.fromisoformat(t.effective_deadline[:10]), "deadline")
            except ValueError:
                return None
        if t.kind == "일정" and t.effective_schedule:
            start = t.effective_schedule.get("start")
            if isinstance(start, str):
                try:
                    moment = datetime.fromisoformat(start.replace("Z", "+00:00"))
                except ValueError:
                    return None
                if moment.tzinfo is not None:
                    return _Due(
                        moment.astimezone(self.read.config.time.zone).date(),
                        "schedule",
                    )
        return None

    def _channel(self, source_id: str) -> str | None:
        archive = self.read.archive
        if archive is None:
            return None
        try:
            return str(archive.get(source_id).channel)
        except (SourceNotFound, OSError):
            return None

    # ---- wiki (read-only) --------------------------------------------------------

    def _store(self) -> WikiStore | None:
        root = self.read.config.state_root
        if not (root / "wiki.sqlite3").is_file() or not (root / "wiki.git").is_dir():
            return None
        return self.read._wiki()

    @staticmethod
    def _page_id(store: WikiStore | None, path: str) -> str:
        if store is not None:
            try:
                return store.page_id(path)
            except _WIKI_ERRORS:
                pass
        return _hash(path.encode())[:24]

    @staticmethod
    def _meta(store: WikiStore | None, key: str, cache: Meta) -> dict[str, JsonValue]:
        if key not in cache:
            try:
                cache[key] = store.metadata(f"pages/{key}.md") if store else {}
            except _WIKI_ERRORS:
                cache[key] = {}
        return cache[key]

    # ---- cards -------------------------------------------------------------------

    def cards(self, at: datetime | None, window: Window) -> list[ResultCard]:
        moment = self._moment(at)
        today = moment.date()
        dictionary = self._dictionary()
        facts = [
            t
            for t in self.read.snapshot().threads
            if t.presentation == "fact" and not t.retracted
        ]
        groups: dict[tuple[date, str], list[MobileThread]] = {}
        for t in facts:
            if window == "today":
                day = self._incoming(t)
                if day != today:
                    continue
            else:
                due = self._due(t)
                if due is None or due.day == today:
                    continue
                if abs((due.day - today).days) > AROUND_DAYS:
                    continue
                day = due.day
            groups.setdefault((day, layout.case_key(t, dictionary)), []).append(t)
        ordered = sorted(
            groups.items(),
            key=lambda item: (
                (abs((item[0][0] - today).days), item[0][0], item[0][1])
                if window == "around"
                else (0, item[1][0].seq, item[0][1])
            ),
        )
        texts = card_texts(self.read.config.state_root / "wiki-digest.sqlite3")
        compiled = self._compiled_seq()
        store = self._store()
        meta: Meta = {}
        return [
            self._card(
                day, key, group, window, today, dictionary, texts, compiled, store, meta
            )
            for (day, key), group in ordered
        ]

    def _card(
        self,
        day: date,
        key: str,
        group: list[MobileThread],
        window: Window,
        today: date,
        dictionary: DictionarySnapshot,
        texts: dict[tuple[str, str], CardText],
        compiled: int,
        store: WikiStore | None,
        meta: Meta,
    ) -> ResultCard:
        group = sorted(group, key=lambda t: t.seq)
        earliest = min(group, key=lambda t: (t.occurred_at, t.seq))
        text = texts.get((day.isoformat(), key))
        counterparts = list(
            dict.fromkeys(t.counterpart for t in group if t.counterpart)
        )
        channels = list(
            dict.fromkeys(
                c for t in group if (c := self._channel(t.source_id)) is not None
            )
        )
        dues = [(d.day, d.kind) for t in group if (d := self._due(t)) is not None]
        if window == "around":
            due: _Due | None = _Due(day, next(k for d, k in dues if d == day))
        else:
            due = _Due(*min(dues, key=lambda item: item[0])) if dues else None
        return ResultCard(
            card_id=hashlib.sha256(f"{day.isoformat()}|{key}".encode()).hexdigest()[
                :24
            ],
            day=day.isoformat(),
            key=key,
            sphere=self._sphere(key),
            headline=text.headline if text else (earliest.content or "").strip() or key,
            bullets=list(text.bullets) if text else [],
            people=len(counterparts),
            sources=len({t.source_id for t in group}),
            counterparts=counterparts[:MAX_COUNTERPARTS],
            channels=channels,
            due=due.day.isoformat() if due else None,
            due_label=layout.due_label(due.day, today) if due else None,
            due_kind=due.kind if due else None,
            thread_ids=[t.thread_id for t in group],
            link=self._link(group, earliest, dictionary, store, meta),
            reflecting=any(t.seq > compiled for t in group),
        )

    def _sphere(self, key: str) -> str:
        if key.startswith("source:"):
            return self.read.config.reference.default_sphere
        return key.split("/", 1)[0]

    def _link(
        self,
        group: list[MobileThread],
        earliest: MobileThread,
        dictionary: DictionarySnapshot,
        store: WikiStore | None,
        meta: Meta,
    ) -> LinkTarget | None:
        for t in group:
            page = layout.page_key(t, dictionary)
            if page is None:
                continue
            bullets = self._meta(store, page, meta).get("bullets")
            for bullet in bullets if isinstance(bullets, list) else []:
                cited = isinstance(bullet, dict) and bullet.get("thread_ids")
                bullet_id = (
                    bullet.get("bullet_id") if isinstance(bullet, dict) else None
                )
                if (
                    isinstance(cited, list)
                    and t.thread_id in cited
                    and isinstance(bullet_id, str)
                ):
                    return LinkTarget(
                        kind="document",
                        document_id=self._page_id(store, f"pages/{page}.md"),
                        bullet_id=bullet_id,
                        block_id=None,
                    )
        if store is None:
            return None
        request = next((t for t in group if t.kind == "요청"), None)
        return LinkTarget(
            kind="digest",
            document_id=self._page_id(store, _day_path(self._incoming(earliest))),
            bullet_id=None,
            block_id=layout.block_id(request.thread_id) if request else None,
        )

    # ---- my open tasks -----------------------------------------------------------

    def tasks(self, at: datetime | None) -> list[TaskItem]:
        today = self._moment(at).date()
        snapshot = self.read.snapshot()
        rows = layout.open_tasks(
            snapshot.threads, snapshot.dictionary or DictionarySnapshot([]), today
        )
        store = self._store()
        meta: Meta = {}
        items: list[TaskItem] = []
        for row in rows:
            if not row.mine or not row.content.strip():
                continue
            document_id = title = None
            link = None
            if row.page_key is not None:
                found = self._meta(store, row.page_key, meta)
                if found.get("page_key") == row.page_key:
                    document_id = self._page_id(store, f"pages/{row.page_key}.md")
                    raw = found.get("title")
                    title = (
                        raw
                        if isinstance(raw, str) and raw.strip()
                        else layout.default_title(row.page_key)
                    )
                    link = LinkTarget(
                        kind="document",
                        document_id=document_id,
                        bullet_id=None,
                        block_id=row.block,
                    )
            items.append(_task_item(row, document_id, title, link))
        return items

    # ---- documents ---------------------------------------------------------------

    def _pages(self, store: WikiStore | None) -> list["_Page"]:
        """Committed documents and Digests; a person's unsettled edit stays hidden."""
        if store is None:
            return []
        try:
            baseline = store.baseline_pages()
        except _WIKI_ERRORS:
            return []
        pages: list[_Page] = []
        for path, data, _ in baseline:
            if not path.endswith(".md"):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            body = text.split("\n---\n", 1)[1] if text.startswith("---\n") else text
            body = body.removeprefix(NOTICE)
            sha = hashlib.sha256(data).hexdigest()
            page_id = self._page_id(store, path)
            if path.startswith("pages/"):
                meta = self._meta(store, path.removeprefix("pages/")[:-3], {})
                # Private properties live in the page's metadata, not its file.
                found = meta.get("page_key")
                if not isinstance(found, str) or "/" not in found:
                    continue
                title = meta.get("title")
                as_of = meta.get("as_of")
                kind = _tree_kind(found)
                pages.append(
                    _Page(
                        path,
                        page_id,
                        found,
                        kind,
                        title
                        if isinstance(title, str) and title.strip()
                        else layout.default_title(found),
                        as_of
                        if isinstance(as_of, str) and _DATE.match(as_of)
                        else None,
                        body,
                        sha,
                        meta,
                    )
                )
            elif path.startswith("Digest/"):
                stem = path.rsplit("/", 1)[-1][:-3]
                heading = next(
                    (
                        line[2:].strip()
                        for line in body.splitlines()
                        if line.startswith("# ")
                    ),
                    "",
                )
                pages.append(
                    _Page(
                        path,
                        page_id,
                        stem,
                        "digest",
                        heading or stem,
                        stem if _DATE.match(stem) else None,
                        body,
                        sha,
                        {},
                    )
                )
        return pages

    def _page_meta(self, store: WikiStore | None, path: str) -> dict[str, JsonValue]:
        return self._meta(store, path.removeprefix("pages/")[:-3], {})

    def references(self, path: str, limit: int = 10) -> list[RefLinkView]:
        title = layout.default_title(path)
        try:
            chunks = find_references(
                self.read.config.state_root, [path], title, limit=limit
            )
        except (sqlite3.Error, ValueError, OSError):
            return []
        links: list[RefLinkView] = []
        for chunk in chunks:
            home = chunk.commit is None
            link = RefLinkView(
                sphere=chunk.sphere,
                path=chunk.path.removesuffix(".md") if home else chunk.path,
                heading=_ref_text(chunk.heading) or Path(chunk.path).stem,
                home=home,
            )
            if link not in links:
                links.append(link)
        return links

    def documents(self) -> list[DocumentSummary]:
        pages = self._pages(self._store())
        return [
            DocumentSummary(
                document_id=p.id,
                kind=p.kind,
                key=p.key,
                title=p.title,
                as_of=p.as_of,
                sha256=p.sha256,
            )
            for p in sorted(pages, key=lambda p: (p.kind == "digest", p.key or ""))
        ]

    def _find(self, document_id: str) -> "_Page":
        for page in self._pages(self._store()):
            if page.id == document_id:
                return page
        raise ApiError(404, "document_not_found")

    def document_key(self, document_id: str) -> str:
        """The page key of a compiled document; Digests take no feedback."""
        page = self._find(document_id)
        if page.key is None or page.kind == "digest":
            raise ApiError(404, "document_not_found")
        return page.key

    def document(self, document_id: str, at: datetime | None = None) -> DocumentView:
        return self._view(self._find(document_id), at)

    def _view(self, page: "_Page", at: datetime | None) -> DocumentView:
        snapshot = self.read.snapshot()
        today = self._moment(at).date()
        compiled = self._compiled_seq()
        if page.kind == "digest":
            sections = self._digest_sections(page, snapshot.threads, today)
            reflecting = bool(page.as_of) and any(
                t.seq > compiled
                and t.presentation == "fact"
                and not t.retracted
                and self._incoming(t).isoformat() == page.as_of
                for t in snapshot.threads
            )
            references: list[RefLinkView] = []
            subject = None
        else:
            assert page.key is not None
            dictionary = snapshot.dictionary or DictionarySnapshot([])
            sections = self._document_sections(
                page, snapshot.threads, dictionary, today
            )
            reflecting = any(
                t.seq > compiled
                and t.presentation == "fact"
                and not t.retracted
                and layout.page_key(t, dictionary) == page.key
                for t in snapshot.threads
            )
            references = self.references(page.key, DOC_REFERENCES)
            subject = page.key
        return DocumentView(
            document_id=page.id,
            kind=page.kind,
            key=page.key,
            title=page.title,
            as_of=page.as_of,
            path=page.path,
            sections=sections,
            references=references,
            log_subject=subject,
            reflecting=reflecting,
            sha256=page.sha256,
        )

    def _document_sections(
        self,
        page: "_Page",
        threads: list[MobileThread],
        dictionary: DictionarySnapshot,
        today: date,
    ) -> list[DocSection]:
        assert page.key is not None
        lines = _split_sections(page.body)
        cited = {
            str(b.get("bullet_id")): b
            for b in _dicts(page.meta.get("bullets"))
            if b.get("bullet_id")
        }
        current = [
            DocBullet(
                bullet_id=f"b{n}",
                text=line[2:].strip(),
                thread_ids=_strings(cited.get(f"b{n}", {}).get("thread_ids")),
                task=None,
                day=None,
                label=None,
            )
            for n, line in enumerate(
                (x for x in lines.get("현재 상태", []) if x.startswith("- ")), 1
            )
            if line[2:].strip()
        ]
        notes = _dicts(page.meta.get("cautions"))
        keyed = any(isinstance(n.get("text"), str) for n in notes)
        cautions: list[DocBullet] = []
        for line in lines.get("주의할 이력", []):
            m = _CAUTION.match(line)
            if m is None:
                continue
            text = m["text"].strip()
            note: dict[str, JsonValue] = {}
            if keyed:
                # Match by shown (date, label, text); a reformatted line simply
                # has no evidence and never shifts the lines after it.
                for i, candidate in enumerate(notes):
                    when = str(candidate.get("date") or "")
                    if (
                        when[5:7].lstrip("0") == m["month"].lstrip("0")
                        and when[8:10].lstrip("0") == m["day"].lstrip("0")
                        and candidate.get("label") == m["label"]
                        and candidate.get("text") == text
                    ):
                        note = notes.pop(i)
                        break
            elif len(cautions) < len(notes):
                note = notes[len(cautions)]  # metadata from before dates were stored
            stored = note.get("date")
            cautions.append(
                DocBullet(
                    bullet_id=str(note.get("candidate_id") or f"c{len(cautions) + 1}"),
                    text=text,
                    thread_ids=_strings(note.get("thread_ids")),
                    task=None,
                    day=stored
                    if isinstance(stored, str) and _DATE.match(stored)
                    else _guess_day(int(m["month"]), int(m["day"]), page.as_of),
                    label=m["label"],
                )
            )
        rows = layout.open_tasks(threads, dictionary, today)
        rest = layout.remaining(page.key, rows)

        def link(row: layout.TaskRow) -> LinkTarget:
            return LinkTarget(
                kind="document",
                document_id=page.id,
                bullet_id=None,
                block_id=row.block,
            )

        mine = [
            DocBullet(
                bullet_id=r.block,
                text=r.content,
                thread_ids=[r.thread_id],
                task=_task_item(r, page.id, page.title, link(r)),
                day=r.deadline.isoformat() if r.deadline else None,
                label=r.due_label,
            )
            for r in rest.mine
            if r.content.strip()
        ]
        waiting = [
            DocBullet(
                bullet_id=r.block,
                text=f"{r.content} · {r.counterpart}" if r.counterpart else r.content,
                thread_ids=[r.thread_id],
                task=None,
                day=r.deadline.isoformat() if r.deadline else None,
                label=r.due_label,
            )
            for r in rest.waiting
            if r.content.strip()
        ]
        wanted: list[
            tuple[
                Literal["current", "remaining", "waiting", "cautions"],
                str,
                list[DocBullet],
            ]
        ] = [
            ("current", "현재 상태", current),
            ("remaining", "남은 일", mine),
            ("waiting", "기다리는 중", waiting),
            ("cautions", "주의할 이력", cautions),
        ]
        return [DocSection(kind=k, title=t, bullets=b) for k, t, b in wanted if b]

    def _digest_sections(
        self, page: "_Page", threads: list[MobileThread], today: date
    ) -> list[DocSection]:
        by_block = {layout.block_id(t.thread_id): t for t in threads}
        tasks: list[DocBullet] = []
        summary: list[DocBullet] = []
        area: Literal["tasks", "summary"] | None = "summary"
        for line in page.body.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
                area = (
                    "tasks"
                    if heading == "들어온 일"
                    else "summary"
                    if heading == "요약"
                    else None
                )
                continue
            m = layout.TASK_LINE.match(line)
            if area == "tasks" and m is not None:
                tasks.append(
                    self._digest_task(page, m, by_block.get(m["block"]), today)
                )
            elif area == "summary" and line.startswith("- "):
                text = " ".join(_WIKILINK.sub("", line[2:]).split())
                if text:
                    summary.append(
                        DocBullet(
                            bullet_id=f"s{len(summary) + 1}",
                            text=text,
                            thread_ids=[],
                            task=None,
                            day=None,
                            label=None,
                        )
                    )
        sections: list[DocSection] = []
        if tasks:
            sections.append(DocSection(kind="tasks", title="들어온 일", bullets=tasks))
        if summary:
            sections.append(DocSection(kind="summary", title="요약", bullets=summary))
        return sections

    @staticmethod
    def _digest_task(
        page: "_Page", m: re.Match[str], found: MobileThread | None, today: date
    ) -> DocBullet:
        due = date.fromisoformat(m["due"]) if m["due"] else None
        done = m["mark"] in "xX"
        task = None
        if found is not None and found.content and found.content.strip():
            task = TaskItem(
                thread_id=found.thread_id,
                revision=found.revision,
                content=found.content,
                block_id=m["block"],
                deadline=due.isoformat() if due else None,
                due_label=layout.due_label(due, today) if due else None,
                counterpart=found.counterpart,
                document_id=page.id,
                document_title=page.title,
                link=LinkTarget(
                    kind="digest",
                    document_id=page.id,
                    bullet_id=None,
                    block_id=m["block"],
                ),
            )
        return DocBullet(
            bullet_id=m["block"],
            text=m["text"].strip() or m["block"],
            thread_ids=[found.thread_id] if found else [],
            task=task,
            day=due.isoformat() if due else None,
            label="완료" if done else (layout.due_label(due, today) if due else None),
        )

    # ---- tree --------------------------------------------------------------------

    def tree(self, at: datetime | None = None) -> list[TreeNodeView]:
        today = self._moment(at).date()
        snapshot = self.read.snapshot()
        dictionary = snapshot.dictionary or DictionarySnapshot([])
        docs = {
            p.key: p.id
            for p in self._pages(self._store())
            if p.kind != "digest" and p.key
        }
        cards: set[tuple[date, str]] = set()
        keys: set[str] = set(docs)
        for t in snapshot.threads:
            if t.presentation != "fact" or t.retracted:
                continue
            key = layout.case_key(t, dictionary)
            if key.startswith("source:"):
                continue
            keys.add(key)
            day = self._incoming(t)
            if 0 <= (today - day).days < TREE_DAYS:
                cards.add((day, key))
        mine = [
            r.page_key
            for r in layout.open_tasks(snapshot.threads, dictionary, today)
            if r.mine and r.page_key
        ]
        nodes: set[str] = set()
        for key in keys:
            parts = key.split("/")
            nodes.update("/".join(parts[:n]) for n in range(1, len(parts) + 1))

        def within(path: str, key: str) -> bool:
            return key == path or key.startswith(path + "/")

        return [
            TreeNodeView(
                path=path,
                label=layout.default_title(path),
                kind=_tree_kind(path),
                parent=path.rsplit("/", 1)[0] if "/" in path else None,
                document_id=docs.get(path),
                open_tasks=sum(1 for k in mine if within(path, k)),
                cards_7d=sum(1 for _, k in cards if within(path, k)),
            )
            for path in sorted(nodes)
        ]

    # ---- evidence ----------------------------------------------------------------

    def _lookup(self) -> SummaryLookup | None:
        if self.read.archive is None:
            return None
        return SummaryLookup(
            self.read.config.archive_root / "summaries", self.read.archive
        )

    def _label(
        self, lookup: SummaryLookup | None, source_id: str, fallback: str
    ) -> str:
        if lookup is not None:
            try:
                return lookup.title(source_id)
            except (SourceNotFound, OSError, ValueError):
                pass
        return fallback if fallback.strip() else source_id

    def _evidence_thread(
        self, lookup: SummaryLookup | None, t: MobileThread
    ) -> EvidenceThread:
        return EvidenceThread(
            thread_id=t.thread_id,
            kind=t.kind,
            content=t.content,
            occurred_at=t.occurred_at,
            source_id=t.source_id,
            source_label=self._label(lookup, t.source_id, t.source),
        )

    def evidence(self, document_id: str, bullet_id: str) -> Evidence:
        page = self._find(document_id)
        view = self._view(page, None)
        bullet = next(
            (b for s in view.sections for b in s.bullets if b.bullet_id == bullet_id),
            None,
        )
        if bullet is None:
            raise ApiError(404, "bullet_not_found")
        threads = self.read.snapshot().threads
        by_id = {t.thread_id: t for t in threads}
        lookup = self._lookup()
        # A retracted Thread is no evidence: neither it nor its source text is shown.
        cited = [
            by_id[i] for i in bullet.thread_ids if i in by_id and not by_id[i].retracted
        ]
        # No phone-side archive policy gate exists: LlmPolicy (weaver.llm.policy)
        # guards text sent to an external model, not reads by the paired phone.
        # If one is added, withhold the script here and set ``truncated``.
        sources: list[EvidenceSource] = []
        for source_id in dict.fromkeys(t.source_id for t in cited):
            own = [
                t.content or ""
                for t in threads
                if t.source_id == source_id
                and t.presentation == "fact"
                and not t.retracted
            ]
            text = None
            if lookup is not None:
                try:
                    found = lookup.latest(source_id)
                except (OSError, ValueError):
                    found = None
                text = found.script if found else None
            if text is None:
                text = "\n".join(own)
            first = next(t for t in cited if t.source_id == source_id)
            sources.append(
                EvidenceSource(
                    source_id=source_id,
                    label=self._label(lookup, source_id, first.source),
                    channel=self._channel(source_id),
                    occurred_at=self._source_time(source_id),
                    text=text[:SOURCE_TEXT_LIMIT],
                    truncated=len(text) > SOURCE_TEXT_LIMIT,
                )
            )
        return Evidence(
            document_id=document_id,
            bullet_id=bullet_id,
            text=bullet.text,
            threads=[self._evidence_thread(lookup, t) for t in cited],
            sources=sources,
        )

    def _source_time(self, source_id: str) -> datetime | None:
        archive = self.read.archive
        if archive is None:
            return None
        try:
            return archive.get(source_id).occurred_at
        except (SourceNotFound, OSError):
            return None

    # ---- source log --------------------------------------------------------------

    def sources(
        self,
        cursor: str | None,
        limit: int,
        sphere: str | None,
        person: str | None,
        kind: str | None,
    ) -> tuple[list[SourceLogEntry], str | None]:
        if cursor in (None, ""):
            offset = 0
        elif cursor and _CURSOR.fullmatch(cursor):
            offset = int(cursor[1:])
        else:
            raise ApiError(422, "invalid_cursor")
        snapshot = self.read.snapshot()
        dictionary = snapshot.dictionary or DictionarySnapshot([])
        groups: dict[str, list[MobileThread]] = {}
        for t in snapshot.threads:
            if t.presentation == "fact" and not t.retracted:
                groups.setdefault(t.source_id, []).append(t)

        def facts(group: list[MobileThread]) -> tuple[list[str], list[str], list[str]]:
            paths = list(
                dict.fromkeys(
                    p for t in group for p in layout._tree_paths(t, dictionary)
                )
            )
            spheres = list(dict.fromkeys(root_of(p) for p in paths))
            people = list(
                dict.fromkeys(
                    n for t in group for n in [t.counterpart, *t.mentions] if n
                )
            )
            return spheres, paths, people

        picked: list[tuple[str, list[MobileThread]]] = []
        for source_id, group in groups.items():
            spheres, _, people = facts(group)
            if sphere and sphere not in spheres:
                continue
            if person and person not in people:
                continue
            if kind and not any(t.kind == kind for t in group):
                continue
            picked.append((source_id, sorted(group, key=lambda t: t.seq)))
        picked.sort(
            key=lambda item: (min(t.occurred_at for t in item[1]), item[0]),
            reverse=True,
        )
        window = picked[offset : offset + limit]
        more = offset + limit < len(picked)
        lookup = self._lookup()
        store = self._store()
        pages = self._pages(store)
        cited: dict[str, list[tuple[_Page, str | None]]] = {}
        for page in pages:
            if page.kind == "digest":
                continue
            for entry in (
                *_dicts(page.meta.get("bullets")),
                *_dicts(page.meta.get("cautions")),
            ):
                bullet = str(entry.get("bullet_id") or entry.get("candidate_id") or "")
                for thread_id in _strings(entry.get("thread_ids")):
                    cited.setdefault(thread_id, []).append((page, bullet or None))
        daily = {p.as_of: p for p in pages if p.kind == "digest" and p.as_of}
        entries: list[SourceLogEntry] = []
        for source_id, group in window:
            spheres, paths, people = facts(group)
            used: list[UsedIn] = []
            seen: set[str] = set()
            for t in group:
                for page, used_bullet in cited.get(t.thread_id, []):
                    if page.id not in seen:
                        seen.add(page.id)
                        used.append(
                            UsedIn(
                                kind="document",
                                document_id=page.id,
                                title=page.title,
                                bullet_id=used_bullet,
                                block_id=None,
                            )
                        )
            for t in group:
                day_page = daily.get(self._incoming(t).isoformat())
                if day_page is not None and day_page.id not in seen:
                    seen.add(day_page.id)
                    request = next(
                        (
                            x
                            for x in group
                            if x.kind == "요청"
                            and daily.get(self._incoming(x).isoformat()) is day_page
                        ),
                        None,
                    )
                    used.append(
                        UsedIn(
                            kind="digest",
                            document_id=day_page.id,
                            title=day_page.title,
                            bullet_id=None,
                            block_id=layout.block_id(request.thread_id)
                            if request
                            else None,
                        )
                    )
            entries.append(
                SourceLogEntry(
                    source_id=source_id,
                    label=self._label(lookup, source_id, group[0].source),
                    channel=self._channel(source_id),
                    occurred_at=min(t.occurred_at for t in group),
                    spheres=spheres,
                    paths=paths,
                    people=people,
                    threads=[self._evidence_thread(lookup, t) for t in group],
                    used_in=used,
                )
            )
        return entries, f"o{offset + limit}" if more else None


def _task_item(
    row: layout.TaskRow,
    document_id: str | None,
    title: str | None,
    link: LinkTarget | None,
) -> TaskItem:
    return TaskItem(
        thread_id=row.thread_id,
        revision=row.revision,
        content=row.content,
        block_id=row.block,
        deadline=row.deadline.isoformat() if row.deadline else None,
        due_label=row.due_label,
        counterpart=row.counterpart,
        document_id=document_id,
        document_title=title,
        link=link,
    )


def _bookmark(path: Path) -> int:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT seq FROM bookmark").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


@dataclass(frozen=True)
class _Page:
    path: str
    id: str
    key: str | None
    kind: DocKind
    title: str
    as_of: str | None
    body: str
    sha256: str
    meta: dict[str, JsonValue]


def _split_sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif current is not None and line.strip():
            current.append(line)
    return sections


def _dicts(value: JsonValue | None) -> list[dict[str, JsonValue]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _strings(value: JsonValue | None) -> list[str]:
    return (
        [v for v in value if isinstance(v, str) and v.strip()]
        if isinstance(value, list)
        else []
    )


def _guess_day(month: int, day: int, as_of: str | None) -> str | None:
    """The caution line shows only month/day; the year comes from the page date."""
    if as_of is None:
        return None
    ref = date.fromisoformat(as_of)
    try:
        found = date(ref.year, month, day)
        return (found if found <= ref else date(ref.year - 1, month, day)).isoformat()
    except ValueError:
        return None


def _ref_text(value: str) -> str:
    return " ".join(re.sub(r"[\[\]|]", " ", value).split())


def _tree_kind(path: str) -> Literal["area", "project", "topic"]:
    kind = node_kind(path)
    return kind if kind in ("area", "project", "topic") else "topic"
