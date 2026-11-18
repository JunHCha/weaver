"""Phone read model: the web's fact/history, mention and effective-value rules.

Everything here is derived on request from Fabric, the context dictionary and
published Wiki pages, using read-only opens. Classification is delegated to
``weaver.web.presentation`` and ``weaver.fabric.state.project`` so the phone and
the web cannot drift apart.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, JsonValue

from weaver.config import ServerConfig
from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.fabric.state import InvalidSetOperation, project
from weaver.schema import Thread
from weaver.web.editing import canonical_threads
from weaver.web.presentation import presentation
from weaver.wiki.store import WikiStore, _properties

from .errors import ApiError

Presentation = Literal["fact", "history"]
Membership = Literal["counterpart", "mention", "area"]
PARTY_KINDS = ("person", "organization")
MAX_PAGE = 500


class MobileThread(Thread):
    presentation: Presentation
    open: bool
    retracted: bool
    effective_deadline: str | None
    effective_schedule: dict[str, JsonValue] | None


class ThreadPage(BaseModel):
    items: list[MobileThread]
    next_cursor: int
    has_more: bool
    head: int


class TagView(BaseModel):
    name: str
    kind: str | None
    archived: bool


class NowEntry(BaseModel):
    thread: MobileThread
    due_kind: Literal["deadline", "schedule"] | None


class NowView(BaseModel):
    today: str
    overdue: list[NowEntry]
    due_today: list[NowEntry]
    recent: list[NowEntry]


class TimelineRow(BaseModel):
    thread: MobileThread
    membership: Membership
    history: bool


class WikiSummary(BaseModel):
    page_id: str
    title: str
    kind: str | None
    area: str | None
    updated_at: datetime
    sha256: str


class WikiPage(WikiSummary):
    frontmatter: dict[str, JsonValue]
    body: str


def history_target(t: Thread) -> str | None:
    return (
        (t.correction.target if t.correction else None)
        or t.retracts
        or t.executes
        or t.resolves
        or t.updates
    )


class Relations:
    """Same as web ``relations()``: a resolve counts unless retracted itself."""

    def __init__(self, rows: list[Thread]) -> None:
        self.retracted = {t.retracts for t in rows if t.retracts}
        self.resolved = {
            t.resolves
            for t in rows
            if t.resolves and not t.retracts and t.thread_id not in self.retracted
        }

    def is_open(self, t: Thread) -> bool:
        return (
            t.kind == "요청"
            and t.thread_id not in self.retracted
            and t.thread_id not in self.resolved
        )


def _root(row: Thread, by_id: Mapping[str, Thread]) -> Thread:
    current, seen = row, {row.thread_id}
    while presentation(current) == "history":
        target = history_target(current)
        nxt = by_id.get(target) if target else None
        if nxt is None or nxt.thread_id in seen:
            break
        seen.add(nxt.thread_id)
        current = nxt
    return current


def _object(value: JsonValue) -> dict[str, JsonValue] | None:
    return value if isinstance(value, dict) else None


def decorate(rows: list[Thread]) -> list[MobileThread]:
    """Attach presentation, open state and effective deadline/schedule."""
    relation = Relations(rows)
    by_id = {t.thread_id: t for t in rows}
    deadline: dict[str, str | None] = {}
    schedule: dict[str, dict[str, JsonValue] | None] = {}
    for t in rows:
        if presentation(t) == "fact":
            deadline[t.thread_id] = (
                t.value if t.field == "deadline" and isinstance(t.value, str) else None
            )
            schedule[t.thread_id] = _object(t.value) if t.field == "schedule" else None
    changes = sorted(
        (
            t
            for t in rows
            if presentation(t) == "history"
            and t.retracts is None
            and t.thread_id not in relation.retracted
            and t.field in ("deadline", "schedule")
            and t.value is not None
        ),
        key=lambda t: (t.recorded_at, t.seq),
    )
    for change in changes:
        root = _root(change, by_id)
        if presentation(root) != "fact" or root.thread_id == change.thread_id:
            continue
        if change.field == "deadline" and isinstance(change.value, str):
            deadline[root.thread_id] = change.value
        elif change.field == "schedule" and (value := _object(change.value)):
            schedule[root.thread_id] = value
    return [
        MobileThread(
            **t.model_dump(),
            presentation=presentation(t),
            open=relation.is_open(t),
            retracted=t.thread_id in relation.retracted,
            effective_deadline=deadline.get(t.thread_id),
            effective_schedule=schedule.get(t.thread_id),
        )
        for t in rows
    ]


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


class ReadModel:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config

    # ---- Fabric ----------------------------------------------------------------

    def rows(self) -> list[Thread]:
        path = self.config.state_root / "fabric.sqlite3"
        if not path.is_file():
            return []
        rows = FabricStore.open_readonly(path).entries(
            ThreadQuery(include_executions=True)
        )
        return canonical_threads(self.config, rows)

    def raw_rows(self) -> list[Thread]:
        path = self.config.state_root / "fabric.sqlite3"
        if not path.is_file():
            return []
        return FabricStore.open_readonly(path).entries(
            ThreadQuery(include_executions=True)
        )

    def head(self) -> int:
        return max((t.seq for t in self.rows()), default=0)

    def threads(self, after_seq: int, limit: int) -> ThreadPage:
        if not 1 <= limit <= MAX_PAGE or after_seq < 0:
            raise ApiError(422, "invalid_page")
        rows = self.rows()
        decorated = decorate(rows)
        selected = sorted(
            (t for t in decorated if t.seq > after_seq), key=lambda t: t.seq
        )
        page = selected[:limit]
        return ThreadPage(
            items=page,
            next_cursor=page[-1].seq if page else after_seq,
            has_more=len(selected) > limit,
            head=max((t.seq for t in rows), default=0),
        )

    def thread(self, thread_id: str) -> MobileThread:
        for item in decorate(self.rows()):
            if item.thread_id == thread_id:
                return item
        raise ApiError(404, "thread_not_found")

    def tags(self) -> list[TagView]:
        path = self.config.state_root / "context.sqlite3"
        dictionary = (
            ContextDictionary.open_readonly(path).snapshot() if path.is_file() else None
        )
        # Like the web tag list: tags as written, plus the canonical entry names.
        names = sorted({tag for t in self.raw_rows() for tag in t.tags})
        result: list[TagView] = []
        for name in names:
            entry = dictionary.resolve(name) if dictionary else None
            result.append(
                TagView(
                    name=name,
                    kind=entry.kind if entry else None,
                    archived=entry.archived if entry else False,
                )
            )
            if entry and entry.name != name and entry.name not in names:
                names.append(entry.name)
        return sorted(result, key=lambda tag: tag.name)

    def states(self, tags: list[str] | None) -> dict[str, JsonValue]:
        rows = self.rows()
        result: dict[str, JsonValue] = {}
        for name in tags or sorted({tag for t in rows for tag in t.tags}):
            try:
                state = project(name, rows)
            except InvalidSetOperation:
                raise ApiError(500, "invalid_state") from None
            result[name] = {
                **{
                    key: state.fields[key].value if key in state.fields else None
                    for key in ("deadline", "schedule", "status")
                },
                "assignees": list[JsonValue](sorted(state.assignees)),
                "open_requests": [t.thread_id for t in state.open_requests],
            }
        return result

    # ---- 지금 --------------------------------------------------------------------

    def now(self, at: datetime | None = None) -> NowView:
        zone = self.config.time.zone
        moment = (at or datetime.now(zone)).astimezone(zone)
        today = moment.date()
        facts = [
            t
            for t in decorate(self.rows())
            if t.presentation == "fact"
            and t.thread_id not in {x.retracts for x in self.rows() if x.retracts}
        ]

        def schedule_start(t: MobileThread) -> datetime | None:
            value = (t.effective_schedule or {}).get("start")
            if not isinstance(value, str):
                return None
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                    zone
                )
            except ValueError:
                return None

        def due(t: MobileThread) -> date | None:
            return _day(t.effective_deadline) if t.effective_deadline else None

        open_requests = [t for t in facts if t.open]
        overdue = sorted(
            (t for t in open_requests if (d := due(t)) is not None and d < today),
            key=lambda t: (due(t) or today, t.seq),
        )
        scheduled_today = [
            t
            for t in facts
            if t.kind == "일정"
            and (s := schedule_start(t)) is not None
            and s.date() == today
        ]
        due_today = [
            *[
                NowEntry(thread=t, due_kind="deadline")
                for t in open_requests
                if due(t) == today
            ],
            *[
                NowEntry(thread=t, due_kind="schedule")
                for t in sorted(
                    scheduled_today,
                    key=lambda t: schedule_start(t) or moment,
                )
            ],
        ]
        listed = {t.thread_id for t in overdue} | {
            e.thread.thread_id for e in due_today
        }
        since = datetime.combine(today - timedelta(days=1), time.min, zone)
        recent = sorted(
            (
                t
                for t in facts
                if t.thread_id not in listed and t.recorded_at.astimezone(zone) >= since
            ),
            key=lambda t: (t.recorded_at, t.seq),
            reverse=True,
        )
        return NowView(
            today=today.isoformat(),
            overdue=[NowEntry(thread=t, due_kind="deadline") for t in overdue],
            due_today=due_today,
            recent=[NowEntry(thread=t, due_kind=None) for t in recent],
        )

    # ---- people / area timelines ---------------------------------------------------

    def timeline(
        self, subject: str, mentions: bool, history: bool
    ) -> list[TimelineRow]:
        tags = {tag.name: tag.kind for tag in self.tags()}
        path = self.config.state_root / "context.sqlite3"
        if path.is_file():
            # A counterpart who is never a tag is still a person in the dictionary.
            dictionary = ContextDictionary.open_readonly(path).snapshot()
            tags |= {entry.name: entry.kind for entry in dictionary.entries}
            known = dictionary.resolve(subject)
            subject = known.name if known else subject
        parties = {name for name, kind in tags.items() if kind in PARTY_KINDS}
        is_party = subject in parties
        decorated = decorate(self.rows())
        by_id = {t.thread_id: t for t in decorated}

        def membership(t: MobileThread) -> Membership | None:
            if is_party and t.counterpart == subject:
                return "counterpart"
            if is_party and subject in t.tags:
                return "mention" if mentions else None
            if not is_party and subject in t.tags:
                return "area"
            return None

        rows: list[TimelineRow] = []
        for t in decorated:
            own = membership(t)
            via = None
            if own is None and t.presentation == "history":
                via = membership(by_id.get(_root(t, by_id).thread_id, t))
            kind = own or via
            if kind is not None and (history or t.presentation == "fact"):
                rows.append(
                    TimelineRow(
                        thread=t, membership=kind, history=t.presentation == "history"
                    )
                )
        return sorted(
            rows, key=lambda r: (r.thread.occurred_at, r.thread.seq), reverse=True
        )

    # ---- Wiki --------------------------------------------------------------------

    def _wiki(self) -> WikiStore:
        return WikiStore.open_readonly(
            self.config.wiki.vault_root or self.config.vault_root,
            self.config.state_root,
            wiki_root=self.config.wiki.folder,
        )

    def _pages(self) -> list[tuple[str, WikiPage]]:
        store = self._wiki()
        if not (self.config.state_root / "wiki.sqlite3").is_file():
            return []
        pages = []
        for path, data, modified in store.baseline_pages():
            if not path.startswith("pages/") or not path.endswith(".md"):
                continue
            try:
                properties = _properties(data)
                text = data.decode("utf-8")
                meta = store.metadata(path)
            except (ValueError, UnicodeDecodeError, OSError):
                continue
            body = text.split("\n---\n", 1)[1] if text.startswith("---\n") else text
            kind = meta.get("kind")
            area = properties.get("area")
            pages.append(
                (
                    path,
                    WikiPage(
                        page_id=store.page_id(path),
                        title=path.removeprefix("pages/").removesuffix(".md"),
                        kind=kind if isinstance(kind, str) else None,
                        area=area if isinstance(area, str) else None,
                        updated_at=modified,
                        sha256=hashlib.sha256(data).hexdigest(),
                        frontmatter={
                            k: v for k, v in properties.items() if k != "weaver"
                        },
                        body=body,
                    ),
                )
            )
        return pages

    def wiki_pages(self) -> list[WikiSummary]:
        return sorted(
            (
                WikiSummary(**page.model_dump(exclude={"frontmatter", "body"}))
                for _, page in self._pages()
            ),
            key=lambda p: p.title,
        )

    def wiki_page(self, page_id: str) -> WikiPage:
        for _, page in self._pages():
            if page.page_id == page_id:
                return page
        raise ApiError(404, "page_not_found")


def json_text(value: BaseModel) -> str:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
