"""Pure placement rules for wiki documents and the Digest; no I/O, no LLM."""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, tzinfo
from typing import Literal, Protocol

from weaver.context import DictionarySnapshot
from weaver.context.tree import UNCLASSIFIED, area_label, node_kind
from weaver.schema.models import Thread

SECTIONS = (
    "## 현재 상태",
    "## 남은 일",
    "## 기다리는 중",
    "## 주의할 이력",
    "## 관련 자료",
)

TASK_LINE = re.compile(
    r"^(?P<indent>\s*)- \[(?P<mark>[ xX])\] (?P<text>.*?)"
    r"(?: 📅 (?P<due>\d{4}-\d{2}-\d{2}))?"
    r"(?: ✅ (?P<done>\d{4}-\d{2}-\d{2}))?"
    r" \^(?P<block>w-[0-9a-f]{10})\s*$"
)
_MAX_CAUTIONS = 5


_PROJECT_PREFIX = re.compile(r"^\d{2}-\d{2}-")


def default_title(key: str) -> str:
    """The document title used until a valid generated one exists."""
    if node_kind(key) == "project":
        last = key.rstrip("/").rsplit("/", 1)[-1]
        return _PROJECT_PREFIX.sub("", last).replace("-", " ")
    return area_label(key)


def block_id(thread_id: str) -> str:
    return "w-" + hashlib.sha256(thread_id.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class TaskMark:
    block: str
    checked: bool
    done: date | None


def task_line(
    content: str, block: str, *, deadline: date | None, done: date | None
) -> str:
    text = " ".join(content.splitlines())
    mark = "x" if done is not None else " "
    line = f"- [{mark}] {text}"
    if deadline is not None:
        line += f" 📅 {deadline.isoformat()}"
    if done is not None:
        line += f" ✅ {done.isoformat()}"
    return f"{line} ^{block}"


def parse_task_marks(text: str) -> dict[str, TaskMark]:
    marks: dict[str, TaskMark] = {}
    for line in text.splitlines():
        m = TASK_LINE.match(line)
        if m is None:
            continue
        block = m["block"]
        checked = m["mark"] in "xX"
        try:
            done = date.fromisoformat(m["done"]) if m["done"] else None
        except ValueError:
            # A hand-typed impossible date still counts as ticked, undated.
            done = None
        prior = marks.get(block)
        if prior is not None:
            checked = checked or prior.checked
            done = done or prior.done
        marks[block] = TaskMark(block, checked, done)
    return marks


class _TagSource(Protocol):
    @property
    def tags(self) -> list[str]: ...


def _tree_paths(t: _TagSource, d: DictionarySnapshot) -> list[str]:
    paths: list[str] = []
    for tag in t.tags:
        entry = d.resolve(tag)
        if entry is not None and entry.source == "tree" and entry.name not in paths:
            paths.append(entry.name)
    return paths


def _classified(paths: list[str]) -> list[str]:
    return [p for p in paths if p.split("/")[1:2] != [UNCLASSIFIED] and "/" in p]


def _key_of(paths: list[str], d: DictionarySnapshot) -> str | None:
    for path in paths:
        if node_kind(path) == "project":
            return path
    if not paths:
        return None
    area = d.area_of(paths[0])
    return area.name if area is not None else paths[0]


def page_key(t: Thread, d: DictionarySnapshot) -> str | None:
    return _key_of(_classified(_tree_paths(t, d)), d)


def case_key(t: Thread, d: DictionarySnapshot) -> str:
    paths = _classified(_tree_paths(t, d))
    for path in paths:
        if node_kind(path) == "project":
            return path
    return paths[0] if paths else f"source:{t.source_id}"


def due_label(day: date, today: date) -> str:
    n = (day - today).days
    if n == 0:
        return "오늘"
    if n == 1:
        return "내일"
    if n == -1:
        return "어제"
    return f"D-{n}" if n > 1 else f"{-n}일 전"


@dataclass(frozen=True)
class TaskRow:
    thread_id: str
    revision: int
    content: str
    block: str
    deadline: date | None
    due_label: str | None
    counterpart: str | None
    mine: bool
    page_key: str | None


class _TaskSource(_TagSource, Protocol):
    """The slice of a mobile thread that task extraction reads."""

    @property
    def thread_id(self) -> str: ...
    @property
    def seq(self) -> int: ...
    @property
    def revision(self) -> int: ...
    @property
    def open(self) -> bool: ...
    @property
    def retracted(self) -> bool: ...
    @property
    def presentation(self) -> str: ...
    @property
    def effective_deadline(self) -> str | None: ...
    @property
    def content(self) -> str | None: ...
    @property
    def counterpart(self) -> str | None: ...
    @property
    def direction(self) -> str | None: ...
    @property
    def source_id(self) -> str: ...


def open_tasks(
    rows: Sequence[_TaskSource], d: DictionarySnapshot, today: date
) -> list[TaskRow]:
    picked = [
        t for t in rows if t.open and t.presentation == "fact" and not t.retracted
    ]
    dated: list[tuple[date, int, TaskRow]] = []
    undated: list[tuple[int, TaskRow]] = []
    for t in picked:
        deadline = (
            date.fromisoformat(t.effective_deadline[:10])
            if t.effective_deadline
            else None
        )
        key = _key_of(_classified(_tree_paths(t, d)), d)
        row = TaskRow(
            thread_id=t.thread_id,
            revision=t.revision,
            content=t.content or "",
            block=block_id(t.thread_id),
            deadline=deadline,
            due_label=due_label(deadline, today) if deadline else None,
            counterpart=t.counterpart,
            mine=t.direction == "받음",
            page_key=key,
        )
        if deadline is None:
            undated.append((t.seq, row))
        else:
            dated.append((deadline, t.seq, row))
    dated.sort(key=lambda item: (item[0], item[1]))
    undated.sort(key=lambda item: item[0])
    return [r for _, _, r in dated] + [r for _, r in undated]


@dataclass(frozen=True)
class Remaining:
    mine: list[TaskRow]
    waiting: list[TaskRow]


def remaining(key: str, tasks: list[TaskRow]) -> Remaining:
    own = [t for t in tasks if t.page_key == key]
    return Remaining(
        mine=[t for t in own if t.mine], waiting=[t for t in own if not t.mine]
    )


type CautionLabel = Literal["기한 변경", "담당 변경", "번복", "철회", "위치 정정"]


@dataclass(frozen=True)
class Caution:
    candidate_id: str
    label: CautionLabel
    day: date
    thread_ids: tuple[str, ...]


def _target(t: Thread) -> str | None:
    return (
        (t.correction.target if t.correction else None)
        or t.retracts
        or t.executes
        or t.resolves
        or t.amends
        or t.updates
    )


def _label(t: Thread) -> CautionLabel | None:
    if t.amends or t.field == "deadline":
        return "기한 변경"
    if t.field == "assignees":
        return "담당 변경"
    if t.updates:
        return "번복"
    if t.retracts:
        return "철회"
    if t.kind == "태그정정":
        return "위치 정정"
    return None


def caution_candidates(
    page_threads: list[Thread], rows: list[Thread], zone: tzinfo
) -> list[Caution]:
    ids = {t.thread_id for t in page_threads}
    found: list[tuple[Thread, CautionLabel, str]] = []
    for t in rows:
        target = _target(t)
        if target is None or target not in ids:
            continue
        if t.retracts and t.thread_id.startswith("feedback:"):
            continue
        if (
            t.retracts
            and t.thread_id.startswith("phone-request:")
            and t.retracts.startswith("phone-request:")
        ):
            continue
        label = _label(t)
        if label is not None:
            found.append((t, label, target))
    found.sort(key=lambda item: (item[0].recorded_at, item[0].seq), reverse=True)
    return [
        Caution(
            candidate_id="c-" + hashlib.sha256(t.thread_id.encode()).hexdigest()[:8],
            label=label,
            day=t.recorded_at.astimezone(zone).date(),
            thread_ids=(t.thread_id, target),
        )
        for t, label, target in found[:_MAX_CAUTIONS]
    ]


@dataclass(frozen=True)
class Bullet:
    bullet_id: str
    text: str
    thread_ids: tuple[str, ...]


@dataclass(frozen=True)
class RefLink:
    sphere: str
    path: str
    heading: str
    home: bool


def _task_text(row: TaskRow) -> str:
    return task_line(row.content, row.block, deadline=row.deadline, done=None)


def render_document(
    title: str,
    as_of: date,
    current: list[Bullet],
    rest: Remaining,
    cautions: list[tuple[Caution, str]],
    refs: list[RefLink],
    zone: tzinfo,
) -> str:
    out: list[str] = [f"# {title}", ""]
    out += [SECTIONS[0], "", f"_{as_of.month}월 {as_of.day}일 기준_", ""]
    out += [f"- {b.text}" for b in current]
    if current:
        out.append("")
    if rest.mine:
        out += [SECTIONS[1], "", *(_task_text(r) for r in rest.mine), ""]
    if rest.waiting:
        out += [SECTIONS[2], ""]
        for r in rest.waiting:
            content = f"{r.content} · {r.counterpart}" if r.counterpart else r.content
            out.append(task_line(content, r.block, deadline=r.deadline, done=None))
        out.append("")
    if cautions:
        out += [SECTIONS[3], ""]
        out += [
            f"- **{c.day.month}/{c.day.day}** [{c.label}] {text}"
            for c, text in cautions
        ]
        out.append("")
    if refs:
        out += [SECTIONS[4], ""]
        for ref in refs:
            if ref.home:
                out.append(f"- [[{ref.path}|{ref.heading}]]")
            else:
                out.append(f"- `회사 vault` {ref.path} › {ref.heading}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"
