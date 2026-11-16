"""Explicit, journaled migration of approved facts to canonical dictionary tags.

Planning opens stores read-only. A source's complete replacement is one Fabric
append; the durable plan is replayed after interruption without another LLM call.
Bump RUN_KEY when deliberately changing extraction rules for another migration.
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from weaver.context import ContextDictionary, ContextEntry, DictionarySnapshot
from weaver.fabric import FabricStore, ThreadQuery
from weaver.llm.types import LlmClient
from weaver.loom.summary_review import ApprovalExtractor, NewEntry
from weaver.schema import (
    ReviewPackage,
    ScheduleValue,
    TagCorrection,
    Thread,
    ThreadDraft,
    Verdict,
)
from weaver.wiki.store import _properties

RUN_KEY = "canonical-tags-v1"


def _id(key: str) -> str:
    return "thr_" + hashlib.sha256(key.encode()).hexdigest()


def _read(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


class Bootstrap(BaseModel):
    entries: list[NewEntry] = Field(default_factory=list)


class HumanChange(BaseModel):
    thread_id: str
    old_tags: list[str]
    new_tags: list[str]


class SourcePlan(BaseModel):
    source_id: str
    key: str
    old: list[Thread] = Field(default_factory=list)
    new: list[ThreadDraft] = Field(default_factory=list)
    additions: list[NewEntry] = Field(default_factory=list)
    human_changes: list[HumanChange] = Field(default_factory=list)
    batch: list[ThreadDraft] = Field(default_factory=list)
    done: bool = False
    error: str | None = None


class Report(BaseModel):
    run_key: str = RUN_KEY
    applied: bool = False
    bootstrap: list[NewEntry] = Field(default_factory=list)
    sources: list[SourcePlan] = Field(default_factory=list)


class MemoryDictionary:
    def __init__(self, entries: list[ContextEntry]) -> None:
        self.entries = entries

    def snapshot(self) -> DictionarySnapshot:
        return DictionarySnapshot(self.entries)

    def add(self, entries: list[NewEntry]) -> None:
        for entry in entries:
            snapshot = self.snapshot()
            if snapshot.resolve(entry.name):
                continue
            self.entries.append(
                ContextEntry(
                    name=entry.name,
                    kind=entry.kind,
                    aliases=tuple(a for a in entry.aliases if not snapshot.resolve(a)),
                    source="learn",
                )
            )


def _wiki_entries(root: Path) -> list[NewEntry]:
    """Read page titles/frontmatter only; summaries are source copies, not names."""
    result = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if (
            any(part.startswith(".") for part in relative.parts)
            or relative.parts[0] == "summaries"
        ):
            continue
        properties = _properties(path.read_bytes())
        name = properties.get("title") or path.stem
        if not isinstance(name, str):
            name = path.stem
        kind = properties.get("kind", "topic")
        if kind not in ("person", "organization", "project", "topic"):
            kind = "topic"
        aliases = properties.get("aliases", [])
        result.append(
            NewEntry.model_validate(
                {
                    "name": name,
                    "kind": kind,
                    "aliases": [a for a in aliases if isinstance(a, str)]
                    if isinstance(aliases, list)
                    else [],
                }
            )
        )
    return result


def _bootstrap(
    llm: LlmClient, memory: MemoryDictionary, threads: list[Thread], wiki_root: Path
) -> list[NewEntry]:
    pages = _wiki_entries(wiki_root)
    # Page canonical titles always survive, including when a tag resembles one.
    memory.add(pages)
    tags = sorted(
        {tag for t in threads for tag in t.tags if not memory.snapshot().resolve(tag)}
    )
    proposed: list[NewEntry] = []
    if tags or pages:
        response = llm.generate_json(
            "loom.reextract_bootstrap",
            "기존 태그를 프로젝트·사람·조직·주제의 짧은 표준 이름 사전으로 정리한다. "
            "TODO, 확인 같은 무의미한 상태 태그는 버린다. 원래 태그 표기는 aliases에 넣는다. "
            "wiki 페이지 title은 바꾸지 말고 명백한 짧은 별칭만 aliases에 추가한다. "
            "기존 사전 표준 이름을 존중한다. entries만 반환한다.",
            json.dumps(
                {
                    "tags": tags,
                    "wiki_pages": [p.model_dump() for p in pages],
                    "dictionary": [
                        {"name": e.name, "kind": e.kind, "aliases": e.aliases}
                        for e in memory.entries
                    ],
                },
                ensure_ascii=False,
            ),
            Bootstrap,
        )
        proposed = response.entries
    # Merge aliases for page titles without renaming them or existing entries.
    for page in pages:
        for candidate in proposed:
            if candidate.name == page.name:
                page.aliases = list(dict.fromkeys([*page.aliases, *candidate.aliases]))
    return [*pages, *(p for p in proposed if p.name not in {e.name for e in pages})]


def _active(threads: list[Thread]) -> list[Thread]:
    retracted = {t.retracts for t in threads if t.retracts}
    return [
        t
        for t in threads
        if t.thread_id not in retracted
        and t.retracts is None
        and t.kind not in ("거절", "실행", "태그정정")
    ]


def _aux(
    key: str, target: Thread, now: datetime, *, tags: list[str] | None = None
) -> ThreadDraft:
    return ThreadDraft(
        thread_id=_id(key),
        kind="기록" if tags is None else "태그정정",
        content="재추출에 따른 기존 Thread 철회"
        if tags is None
        else "사전 표준 이름으로 태그 정정",
        occurred_at=now,
        recorded_at=now,
        tags=[],
        field=None,
        value=None,
        source_id=target.source_id,
        actor="system" if tags is None else "human",
        direction=None,
        resolves=None,
        retracts=target.thread_id if tags is None else None,
        correction=None
        if tags is None
        else TagCorrection(target=target.thread_id, remove=target.tags, add=tags),
    )


def _replace(plan: SourcePlan, drafts: list[ThreadDraft], now: datetime) -> None:
    ids = {t.thread_id: _id(f"{plan.key}:new:{i}") for i, t in enumerate(drafts)}
    available = [t for t in plan.old if t.kind == "일정" and t.value is not None]
    for draft in drafts:
        data = draft.model_dump()
        data["thread_id"] = ids[draft.thread_id]
        for field in ("blocked_by", "updates", "resolves", "retracts"):
            data[field] = ids.get(data[field], data[field])
        # Re-extraction can change wording/tags, but an identical time slot is
        # still the same event. Consume each old schedule at most once.
        if draft.kind == "일정" and draft.value is not None:
            value = ScheduleValue.model_validate(draft.value)
            match = next(
                (
                    t
                    for t in available
                    if (old := ScheduleValue.model_validate(t.value)).start
                    == value.start
                    and old.end == value.end
                ),
                None,
            )
            if match is not None:
                data["updates"] = match.thread_id
                available.remove(match)
        plan.new.append(ThreadDraft.model_validate(data))
    plan.batch = [
        _aux(f"{plan.key}:retract:{t.thread_id}", t, now) for t in plan.old
    ] + plan.new


def _saved(root: Path, key: str) -> SourcePlan | None:
    path = root / "reextract.sqlite3"
    if not path.exists():
        return None
    with closing(_read(path)) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plans'"
        ).fetchone():
            return None
        row = db.execute(
            "SELECT payload, done FROM plans WHERE key=?", (key,)
        ).fetchone()
    if row is None:
        return None
    plan = SourcePlan.model_validate_json(row[0])
    plan.done = bool(row[1])
    return plan


def _journal(root: Path, plan: SourcePlan) -> None:
    path = root / "reextract.sqlite3"
    # Private before SQLite creates its rollback journal.
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA fullfsync=ON")
        db.execute(
            "CREATE TABLE IF NOT EXISTS plans (key TEXT PRIMARY KEY, payload TEXT NOT NULL, done INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT OR IGNORE INTO plans VALUES (?, ?, 0)",
            (plan.key, plan.model_dump_json()),
        )


def _apply(root: Path, fabric: FabricStore, plan: SourcePlan) -> None:
    _journal(root, plan)
    # Fabric is the atomic boundary. Recovery replays this exact payload, then
    # finishes idempotent dictionary additions even after a lost append reply.
    fabric.append(plan.batch)
    context = ContextDictionary(root)
    for entry in plan.additions:
        context.add_entry(
            entry.name,
            entry.kind,
            tuple(entry.aliases),
            human_edited=False,
            drop_conflicting_aliases=True,
        )
    with closing(sqlite3.connect(root / "reextract.sqlite3")) as db, db:
        db.execute("UPDATE plans SET done=1 WHERE key=?", (plan.key,))
    plan.done = True


def reextract(
    state_root: Path,
    wiki_root: Path,
    llm: LlmClient,
    *,
    apply: bool = False,
    source: str | None = None,
    timezone: str = "Asia/Seoul",
) -> Report:
    """Plan/replay each source independently; caller holds daemon lock on apply."""
    fabric = FabricStore.open_readonly(state_root / "fabric.sqlite3")
    threads = fabric.entries(ThreadQuery(include_executions=True))
    path = state_root / "context.sqlite3"
    entries = (
        ContextDictionary.open_readonly(path).snapshot().list_entries()
        if path.exists()
        else []
    )
    memory = MemoryDictionary(entries.copy())
    report = Report(applied=apply)
    with closing(_read(state_root / "summary.sqlite3")) as db:
        rows = db.execute(
            "SELECT p.package, p.script, r.verdict, r.prepared FROM packages p JOIN reviews r ON p.id=r.id AND p.revision=r.revision WHERE r.stage='APPLIED' ORDER BY p.id, p.revision DESC"
        ).fetchall()
    selected: dict[str, tuple[ReviewPackage, str, Verdict]] = {}
    extracted_ids: dict[str, set[str]] = {}
    for raw, script, raw_verdict, raw_prepared in rows:
        # Historical feedback revisions are not extraction inputs.
        if json.loads(raw_verdict).get("action") not in ("approve", "edit"):
            continue
        verdict = Verdict.model_validate_json(raw_verdict)
        package = ReviewPackage.model_validate_json(raw)
        extracted_ids.setdefault(package.source_id, set()).update(
            t["thread_id"] for t in json.loads(raw_prepared).get("threads", [])
        )
        if verdict.action in ("approve", "edit") and (
            source is None or package.source_id == source
        ):
            selected.setdefault(package.source_id, (package, script, verdict))
    active = _active(threads)
    sources = sorted(
        set(selected)
        | {
            t.source_id
            for t in active
            if t.actor == "human" and (source is None or t.source_id == source)
        }
    )
    # Only bootstrap when work remains; a completed rerun needs no paid calls.
    pending = []
    for source_id in sources:
        revision = selected[source_id][0] if source_id in selected else None
        key = f"{RUN_KEY}:{source_id}:{revision.package_id + ':' + str(revision.revision) if revision else 'human'}"
        saved = _saved(state_root, key)
        if saved:
            report.sources.append(saved)
            if apply and not saved.done:
                try:
                    _apply(
                        state_root, FabricStore(state_root / "fabric.sqlite3"), saved
                    )
                except Exception as error:
                    saved.error = type(error).__name__
            continue
        pending.append((source_id, key))
    if not pending:
        return report
    report.bootstrap = _bootstrap(llm, memory, threads, wiki_root)
    # Rebuild from original dictionary so page aliases take part in resolution.
    memory = MemoryDictionary(entries.copy())
    memory.add(report.bootstrap)
    writable = FabricStore(state_root / "fabric.sqlite3") if apply else None
    for source_id, key in pending:
        plan = SourcePlan(
            source_id=source_id, key=key, additions=report.bootstrap.copy()
        )
        report.sources.append(plan)
        # A failed source must not seed uncommitted entries into another
        # source's extraction. Each plan owns all of its dictionary additions.
        memory = MemoryDictionary(entries.copy())
        memory.add(report.bootstrap)
        extractor = ApprovalExtractor(llm, memory, fabric, timezone=timezone)
        try:
            now = datetime.now(UTC)
            if source_id in selected:
                package, script, verdict = selected[source_id]
                # Edit approvals have human actor, but belong to the stored
                # extraction, unlike independently authored wiki edits.
                old_ids = extracted_ids.get(source_id, set())
                plan.old = [
                    t
                    for t in active
                    if t.source_id == source_id
                    and (t.actor == "ai_approved" or t.thread_id in old_ids)
                ]
                prepared = extractor.prepare(package, script, verdict, now=now)
                plan.additions.extend(prepared.new_entries)
                memory.add(prepared.new_entries)
                _replace(plan, prepared.threads, now)
            old_ids = {t.thread_id for t in plan.old}
            for thread in active:
                if (
                    thread.source_id != source_id
                    or thread.actor != "human"
                    or thread.thread_id in old_ids
                ):
                    continue
                tags = list(
                    dict.fromkeys(
                        entry.name
                        for tag in thread.tags
                        if (entry := memory.snapshot().resolve(tag))
                    )
                )
                if tags != thread.tags:
                    plan.human_changes.append(
                        HumanChange(
                            thread_id=thread.thread_id,
                            old_tags=thread.tags,
                            new_tags=tags,
                        )
                    )
                    plan.batch.append(
                        _aux(f"{key}:tags:{thread.thread_id}", thread, now, tags=tags)
                    )
            if writable:
                _apply(state_root, writable, plan)
        except Exception as error:
            plan.error = type(error).__name__
    return report


def render(report: Report) -> str:
    lines = [
        "적용" if report.applied else "미리보기 (Weaver 상태·사전·원장 쓰기 없음)",
        "초기 사전 항목: "
        + json.dumps([e.model_dump() for e in report.bootstrap], ensure_ascii=False),
    ]
    for plan in report.sources:
        lines.append(f"출처 {plan.source_id}" + (" (반영됨)" if plan.done else ""))
        for label, items in (("기존 Threads", plan.old), ("새 Threads", plan.new)):
            lines.append(label + (" →" if label == "기존 Threads" else ""))
            for t in items:
                lines.append(
                    f"  {t.kind}: {t.content} | tags={t.tags} | {t.field}={json.dumps(t.value, ensure_ascii=False)}"
                )
        for change in plan.human_changes:
            lines.append(
                f"  사람 작성 {change.thread_id}: {change.old_tags} → {change.new_tags}"
            )
        lines.append(
            "새 사전 항목: "
            + json.dumps([e.model_dump() for e in plan.additions], ensure_ascii=False)
        )
        if plan.error:
            lines.append(f"  오류: {plan.error}")
    return "\n".join(lines)
