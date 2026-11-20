"""Phone read model: the web's fact/history, mention and effective-value rules.

Everything here is derived on request from Fabric, the context dictionary and
published Wiki pages, using read-only opens. Classification is delegated to
``weaver.web.presentation`` and ``weaver.fabric.state.project`` so the phone and
the web cannot drift apart.
"""

import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, JsonValue

from weaver.archive import ArchiveStore, SourceNotFound
from weaver.config import ServerConfig
from weaver.context import ContextDictionary, DictionarySnapshot
from weaver.fabric import FabricStore
from weaver.fabric.requests import request_values
from weaver.fabric.state import InvalidSetOperation, project
from weaver.schema import Thread
from weaver.web.editing import canonical_threads, dictionary_entries
from weaver.web.presentation import presentation
from weaver.wiki.store import WikiStore, _properties

from .errors import ApiError

Presentation = Literal["fact", "history"]
Membership = Literal["counterpart", "mention", "area"]
PARTY_KINDS = ("person", "organization")
MAX_PAGE = 500


class MobileThread(Thread):
    revision: int
    presentation: Presentation
    open: bool
    retracted: bool
    effective_deadline: str | None
    effective_schedule: dict[str, JsonValue] | None
    mentions: list[str]
    source: str


class ThreadPage(BaseModel):
    items: list[MobileThread]
    next_cursor: str | None
    head: int


class TagView(BaseModel):
    name: str
    kind: str | None
    archived: bool


class NowEntry(BaseModel):
    thread: MobileThread
    due_kind: Literal["deadline", "schedule"] | None


class NowView(BaseModel):
    head: int
    fabric_epoch: str
    timezone: str
    generated_at: datetime
    today: str
    overdue: list[NowEntry]
    due_today: list[NowEntry]
    recent: list[NowEntry]


class TimelineRow(BaseModel):
    thread: MobileThread
    membership: Membership
    history: bool


class WikiSummary(BaseModel):
    id: str
    title: str
    kind: str | None
    area: str | None
    updated_at: datetime
    frontmatter: dict[str, JsonValue]
    sha256: str


class WikiPage(WikiSummary):
    body: str


def history_target(t: Thread) -> str | None:
    return (
        (t.correction.target if t.correction else None)
        or t.retracts
        or t.executes
        or t.resolves
        or t.amends
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


def decorate(
    rows: list[Thread],
    parties: frozenset[str] = frozenset(),
    source_of: Callable[[str], str] = lambda _id: "",
) -> list[MobileThread]:
    """Attach presentation, open state, effective values, mentions and source."""
    relation = Relations(rows)
    request_deadlines, revisions = request_values(rows)
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
        key=lambda t: (t.occurred_at, t.recorded_at, t.seq),
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
            revision=revisions.get(t.thread_id, t.seq),
            presentation=presentation(t),
            open=relation.is_open(t),
            retracted=t.thread_id in relation.retracted,
            effective_deadline=request_deadlines.get(
                t.thread_id, deadline.get(t.thread_id)
            ),
            effective_schedule=schedule.get(t.thread_id),
            mentions=[tag for tag in t.tags if tag in parties and tag != t.counterpart],
            source=source_of(t.source_id),
        )
        for t in rows
    ]


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class Snapshot:
    """Everything derived from Fabric and the dictionary at one database state."""

    key: tuple[tuple[int, int], ...]
    rows: list[Thread]
    raw: list[Thread]
    threads: list[MobileThread]  # in seq order
    dictionary: DictionarySnapshot | None
    kinds: dict[str, str | None]
    head: int
    fabric_epoch: str


_SOURCE_LABELS = {
    "call": "통화",
    "voice_memo": "메모",
    "kakao": "카카오",
    "share_text": "공유 텍스트",
    "webex": "Webex",
}


class ReadModel:
    def __init__(
        self,
        config: ServerConfig,
        archive: ArchiveStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.archive = archive
        self.clock = clock or (lambda: datetime.now(config.time.zone))
        self._lock = threading.Lock()
        self._snapshot_cache: Snapshot | None = None
        self._labels: dict[str, str] = {}

    # ---- Fabric ----------------------------------------------------------------

    def _key(self) -> tuple[tuple[int, int], ...]:
        """Cheap change detector: size and mtime of the databases and their WAL."""
        key = []
        for name in ("fabric.sqlite3", "context.sqlite3"):
            for suffix in ("", "-wal"):
                try:
                    info = os.stat(self.config.state_root / (name + suffix))
                    key.append((info.st_mtime_ns, info.st_size))
                    key.append((info.st_dev, info.st_ino))
                except OSError:
                    key.extend(((0, 0), (0, 0)))
        return tuple(key)

    def _label(self, source_id: str) -> str:
        label = self._labels.get(source_id)
        if label is None:
            label = "알 수 없음"
            if self.archive is not None:
                try:
                    source = self.archive.get(source_id)
                    label = (
                        source.hints.counterpart
                        if source.channel in ("call", "kakao")
                        and source.hints.counterpart
                        else _SOURCE_LABELS[source.channel]
                    )
                except (SourceNotFound, OSError):
                    pass
            self._labels[source_id] = label
        return label

    def snapshot(self) -> Snapshot:
        """Decorated threads, rebuilt only when the databases changed.

        Paging through ``/read/threads`` therefore reuses one computation.
        """
        with self._lock:
            key = self._key()
            cached = self._snapshot_cache
            if cached is not None and cached.key == key:
                return cached
            fabric = self.config.state_root / "fabric.sqlite3"
            raw, fabric_epoch = (
                FabricStore.open_readonly(fabric).read_snapshot()
                if fabric.is_file()
                else ([], "empty")
            )
            rows = canonical_threads(self.config, raw)
            context = self.config.state_root / "context.sqlite3"
            dictionary = (
                ContextDictionary.open_readonly(context).snapshot()
                if context.is_file()
                else None
            )
            kinds: dict[str, str | None] = {}
            for name in sorted({tag for t in raw for tag in t.tags}):
                entry = dictionary.resolve(name) if dictionary else None
                kinds[name] = entry.kind if entry else None
            if dictionary:
                kinds |= {entry.name: entry.kind for entry in dictionary.entries}
            parties = frozenset(n for n, k in kinds.items() if k in PARTY_KINDS)
            threads = sorted(decorate(rows, parties, self._label), key=lambda t: t.seq)
            snapshot = Snapshot(
                key,
                rows,
                raw,
                threads,
                dictionary,
                kinds,
                max((t.seq for t in rows), default=0),
                fabric_epoch,
            )
            self._snapshot_cache = snapshot
            return snapshot

    def rows(self) -> list[Thread]:
        return self.snapshot().rows

    def head(self) -> int:
        return self.snapshot().head

    @staticmethod
    def _cursor(raw: str | None) -> int:
        if raw is None or raw == "":
            return 0
        if raw.startswith("c") and raw[1:].isdigit():
            return int(raw[1:])
        raise ApiError(422, "invalid_cursor")

    def threads(self, cursor: str | None, limit: int) -> ThreadPage:
        if not 1 <= limit <= MAX_PAGE:
            raise ApiError(422, "invalid_page")
        after = self._cursor(cursor)
        snapshot = self.snapshot()
        selected = [t for t in snapshot.threads if t.seq > after]
        page = selected[:limit]
        return ThreadPage(
            items=page,
            next_cursor=f"c{page[-1].seq}" if len(selected) > limit else None,
            head=snapshot.head,
        )

    def thread(self, thread_id: str) -> MobileThread:
        for item in self.snapshot().threads:
            if item.thread_id == thread_id:
                return item
        raise ApiError(404, "thread_not_found")

    def dictionary(self) -> list[dict[str, object]]:
        return [
            {
                "name": entry["name"],
                "kind": entry["kind"],
                "aliases": entry["aliases"],
                "usage_count": entry["usage_count"],
                "archived": entry["archived"],
            }
            for entry in dictionary_entries(self.config)
        ]

    def tags(self) -> list[TagView]:
        snapshot = self.snapshot()
        dictionary = snapshot.dictionary
        # Like the web tag list: tags as written, plus the canonical entry names.
        names = sorted({tag for t in snapshot.raw for tag in t.tags})
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

    def now(
        self,
        at: datetime | None = None,
        after_seq: int | None = None,
        fabric_epoch: str | None = None,
    ) -> NowView:
        zone = self.config.time.zone
        moment = (at or self.clock()).astimezone(zone)
        today = moment.date()
        snapshot = self.snapshot()
        if after_seq is not None:
            if fabric_epoch is None:
                raise ApiError(422, "fabric_epoch_required")
            if fabric_epoch != snapshot.fabric_epoch:
                raise ApiError(409, "epoch_mismatch")
            if after_seq < 0 or after_seq > snapshot.head:
                raise ApiError(409, "checkpoint_out_of_range")
        facts = [
            t for t in snapshot.threads if t.presentation == "fact" and not t.retracted
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
        # Changed requests must also convey closure/withdrawal since the user's
        # bookmark. Keep the normal due sections restricted to active facts.
        recent_candidates = (
            facts
            if after_seq is None
            else [
                t
                for t in snapshot.threads
                if t.presentation == "fact"
                and (not t.retracted or (t.kind == "요청" and t.revision > after_seq))
            ]
        )
        recent = sorted(
            (
                t
                for t in recent_candidates
                if t.thread_id not in listed
                and (
                    (t.seq > after_seq or (t.kind == "요청" and t.revision > after_seq))
                    if after_seq is not None
                    else t.recorded_at.astimezone(zone) >= since
                )
            ),
            key=lambda t: (t.recorded_at, t.seq),
            reverse=True,
        )
        return NowView(
            head=snapshot.head,
            fabric_epoch=snapshot.fabric_epoch,
            timezone=str(zone),
            generated_at=moment,
            today=today.isoformat(),
            overdue=[NowEntry(thread=t, due_kind="deadline") for t in overdue],
            due_today=due_today,
            recent=[NowEntry(thread=t, due_kind=None) for t in recent],
        )

    # ---- people / area timelines ---------------------------------------------------

    def timeline(
        self, subject: str, mentions: bool, history: bool
    ) -> list[TimelineRow]:
        snapshot = self.snapshot()
        if snapshot.dictionary is not None:
            known = snapshot.dictionary.resolve(subject)
            subject = known.name if known else subject
        # A counterpart who is never a tag is still a person in the dictionary.
        parties = {n for n, k in snapshot.kinds.items() if k in PARTY_KINDS}
        is_party = subject in parties
        decorated = snapshot.threads
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
                        id=store.page_id(path),
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
                WikiSummary(**page.model_dump(exclude={"body"}))
                for _, page in self._pages()
            ),
            key=lambda p: p.title,
        )

    def wiki_page(self, page_id: str) -> WikiPage:
        for _, page in self._pages():
            if page.id == page_id:
                return page
        raise ApiError(404, "page_not_found")


def json_text(value: BaseModel) -> str:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
