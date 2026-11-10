"""Deterministic, read-only wiki views over a Fabric snapshot."""

from datetime import date, datetime, timedelta

from weaver.context import ContextDictionary
from weaver.fabric.v2 import Fabric, ThreadQuery
from weaver.fabric.v2.state import project
from weaver.schema.v2 import Thread
from weaver.wiki.store import WikiStore


def _text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("|", "\\|")
    )


def _link(thread: Thread) -> str:
    return f"[[{thread.source_id}]]"


def _date(value: object, now: datetime) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        return parsed.astimezone(now.tzinfo).date()
    except ValueError:
        return None


def render_pages(
    fabric: Fabric,
    context: ContextDictionary,
    *,
    now: datetime,
    inactive_days: int = 30,
) -> dict[str, str]:
    """Render pages from one snapshot; calendar boundaries use ``now``'s timezone.

    Deadlines and schedules accept ISO dates or timezone-aware timestamps.
    Contact counts cover the preceding 7 and 30 days up to the injected time.
    """
    if now.utcoffset() is None or inactive_days < 0:
        raise ValueError("now must be timezone-aware and inactive_days nonnegative")
    threads = fabric.entries(ThreadQuery())
    retracted = {t.retracts for t in threads if t.retracts is not None}
    active = [
        t
        for t in threads
        if t.thread_id not in retracted
        and t.retracts is None
        and t.kind not in ("태그정정", "거절")
        and t.occurred_at <= now
    ]
    resolved = {t.resolves for t in active if t.resolves}
    requests = ["# 열린 요청", ""]
    for direction in ("받음", "보냄"):
        requests.extend([f"## {direction}", ""])
        for t in active:
            if (
                t.kind == "요청"
                and t.direction == direction
                and t.thread_id not in resolved
            ):
                requests.append(
                    f"- {t.occurred_at.astimezone(now.tzinfo).date()}: {_text(t.content or '')} — {_link(t)}"
                )
        requests.append("")
    tags = sorted({tag for t in active for tag in t.tags})
    start = now.date() - timedelta(days=now.weekday())
    due: list[tuple[date, str, str, Thread, int]] = []
    for tag in tags:
        state = project(tag, active)
        for field in ("deadline", "schedule"):
            current = state.fields.get(field)
            if current is None:
                continue
            day = _date(current.value, now)
            if day is None or not start <= day < start + timedelta(days=14):
                continue
            source = next(t for t in active if t.thread_id == current.thread_id)
            dates = [
                _date(t.value, now)
                for t in active
                if tag in t.tags and t.field == field
            ]
            moves = sum(
                1
                for a, b in zip(dates, dates[1:], strict=False)
                if a is not None and b is not None and b > a
            )
            due.append((day, tag, field, source, moves))
    deadlines = ["# 다가오는 마감", ""]
    for label, lower in (("이번 주", start), ("다음 주", start + timedelta(days=7))):
        deadlines.extend([f"## {label}", ""])
        for day, tag, field, source, moves in sorted(
            due, key=lambda item: (item[0], item[1], item[2], item[3].seq)
        ):
            if lower <= day < lower + timedelta(days=7):
                marker = f" — {moves}회 미뤄짐" if moves >= 2 else ""
                deadlines.append(
                    f"- {day} {_text(tag)} ({'마감' if field == 'deadline' else '일정'}): {_text(source.content or '')} — {_link(source)}{marker}"
                )
        deadlines.append("")
    contacts = ["# 사람별 연락", ""]
    for tag in tags:
        if context.kind_of(tag) != "person":
            continue
        events = [t for t in active if tag in t.tags]
        latest = max(t.occurred_at for t in events)
        counts = [
            sum(t.occurred_at >= now - timedelta(days=days) for t in events)
            for days in (7, 30)
        ]
        marker = " — 연락 없음" if latest <= now - timedelta(days=inactive_days) else ""
        contacts.append(
            f"- {_text(tag)}: 최근 {latest.astimezone(now.tzinfo).date()}, 7일 {counts[0]}건, 30일 {counts[1]}건{marker}"
        )
    return {
        "열린 요청.md": "\n".join(requests).rstrip() + "\n",
        "다가오는 마감.md": "\n".join(deadlines).rstrip() + "\n",
        "사람별 연락.md": "\n".join(contacts).rstrip() + "\n",
    }


def write_pages(
    fabric: Fabric,
    context: ContextDictionary,
    store: WikiStore,
    *,
    now: datetime,
    inactive_days: int = 30,
) -> None:
    """Write generated pages through WikiStore and register them as read-only."""
    for path, body in render_pages(
        fabric, context, now=now, inactive_days=inactive_days
    ).items():
        try:
            _, _, base_hash = store.read(path)
        except FileNotFoundError:
            base_hash = None
        store.write(path, {}, body, base_hash, readonly=True)
