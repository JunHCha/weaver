"""Bounded read-only migration planning and model-free, fingerprinted apply."""

import hashlib
import json
import os
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from weaver.action.calendar import baseline_schedule_ids, derive_calendar_actions
from weaver.action.outbox import ActionRecord, _record
from weaver.config import ServerConfig
from weaver.context import ContextDictionary, ContextEntry, DictionarySnapshot, _key
from weaver.context.proposals import LocationProposals, ProposalDraft
from weaver.context.tree import UNCLASSIFIED
from weaver.fabric import FabricStore, ThreadQuery
from weaver.llm.policy import UNKNOWN_WIKI_SOURCE, ScopedClient
from weaver.llm.types import LlmClient
from weaver.loom.summary_review import ApprovalExtractor, MissingApprovalTime, Prepared
from weaver.loom.summary_state import read_contact_name, read_source_reference
from weaver.loom.tagging import NewEntry, clean_name
from weaver.readonly import connect_snapshot
from weaver.schema import (
    ReviewPackage,
    ScheduleValue,
    TagCorrection,
    Thread,
    ThreadDraft,
    Verdict,
)
from weaver.wiki.provenance import source_ids as wiki_source_ids
from weaver.wiki.store import WikiStore, _properties

RUN_KEY = "path-tree-v1"
BOOTSTRAP_MAX_ENTRIES = 200
BOOTSTRAP_MAX_BYTES = 32768
JUNK_TAGS = frozenset({"todo", "확인", "확인필요", "tbd"})


def wiki_root(config: ServerConfig) -> Path:
    """Same location as WikiStore in runtime assembly; never the whole vault."""
    return (config.wiki.vault_root or config.vault_root) / config.wiki.folder


def _meaningful(value: str) -> bool:
    return bool(value.strip()) and _key(value) not in JUNK_TAGS


def _id(key: str) -> str:
    return "thr_" + hashlib.sha256(key.encode()).hexdigest()


def _read(path: Path) -> sqlite3.Connection:
    return connect_snapshot(path)


class SnapshotFabric(FabricStore):
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(_read(self.path)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("unsupported Fabric schema version")
            yield db


class SnapshotDictionary(ContextDictionary):
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(_read(self._path)) as db:
            yield db


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _db_fingerprint(path: Path) -> str:
    if not path.is_file():
        return "missing"
    with closing(_read(path)) as db:
        db.execute("BEGIN")
        tables = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        content = []
        for name, sql in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = db.execute(f"SELECT * FROM {quoted}").fetchall()
            content.append((name, sql, sorted(rows, key=repr)))
        return _digest((db.execute("PRAGMA user_version").fetchone(), content))


class _CountingClient:
    """Counts LLM requests made through it (bootstrap accounting)."""

    def __init__(self, inner: LlmClient) -> None:
        self.inner = inner
        self.calls = 0

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        self.calls += 1
        return self.inner.generate_json(task, system, user, schema)

    def generate_text(self, task: str, system: str, user: str) -> str:
        self.calls += 1
        return self.inner.generate_text(task, system, user)


def _signatures(root: Path) -> dict[str, str]:
    threads = SnapshotFabric.open_readonly(root / "fabric.sqlite3").entries(
        ThreadQuery(include_executions=True)
    )
    return {
        "fabric_head": str(max((t.seq for t in threads), default=0)),
        "dictionary": _db_fingerprint(root / "context.sqlite3"),
        "summary": _db_fingerprint(root / "summary.sqlite3"),
        "spinner": _db_fingerprint(root / "spinner.sqlite3"),
        "outbox": _db_fingerprint(root / "action-outbox.sqlite3"),
    }


class NameChange(BaseModel):
    observed: str
    canonical: str


class Bootstrap(BaseModel):
    entries: list[NewEntry] = Field(
        default_factory=list, max_length=BOOTSTRAP_MAX_ENTRIES
    )

    renames: list[NameChange] = Field(
        default_factory=list, max_length=BOOTSTRAP_MAX_ENTRIES
    )
    tag_corrections: list[NameChange] = Field(
        default_factory=list, max_length=BOOTSTRAP_MAX_ENTRIES
    )


class HumanChange(BaseModel):
    thread_id: str
    old_tags: list[str]
    new_tags: list[str]


class CalendarEffect(BaseModel):
    old_thread_id: str | None = None
    new_thread_id: str | None = None
    effect: str
    detail: str = ""


class SourcePlan(BaseModel):
    source_id: str
    key: str
    old: list[Thread] = Field(default_factory=list)
    new: list[ThreadDraft] = Field(default_factory=list)
    additions: list[NewEntry] = Field(default_factory=list)
    renames: list[NameChange] = Field(default_factory=list)
    tag_corrections: list[NameChange] = Field(default_factory=list)
    human_changes: list[HumanChange] = Field(default_factory=list)
    calendar_effects: list[CalendarEffect] = Field(default_factory=list)
    batch: list[ThreadDraft] = Field(default_factory=list)
    skipped: str | None = None
    referencing_seqs: list[int] = Field(default_factory=list)
    done: bool = False
    source_time_estimated: bool = False
    error: str | None = None
    location_proposals: list[ProposalDraft] = Field(default_factory=list)


class Report(BaseModel):
    run_key: str = RUN_KEY
    state_root: str = ""
    plan_id: str = ""
    fingerprints: dict[str, str] = Field(default_factory=dict)
    valid: bool = True
    applied: bool = False
    bootstrap: list[NewEntry] = Field(default_factory=list)
    sources: list[SourcePlan] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    llm_calls_by_day: dict[str, int] = Field(default_factory=dict)
    today: str = ""
    pending_sources: list[str] = Field(default_factory=list)
    unclassified: int = 0


def _unclassified_ids(report: Report) -> list[str]:
    """New Threads tagged ``<root>/미분류``, in plan order."""
    return [
        draft.thread_id
        for plan in report.sources
        if not plan.error and not plan.skipped
        for draft in plan.new
        if any(
            tag.endswith(f"/{UNCLASSIFIED}") and tag.count("/") == 1
            for tag in draft.tags
        )
    ]


def _seal(report: Report) -> Report:
    report.plan_id = _digest(report.model_dump(mode="json", exclude={"plan_id"}))
    return report


class MemoryDictionary:
    """Simulate the exact automatic add_entry/add_alias operations in memory."""

    def __init__(
        self, entries: list[ContextEntry], blocked: dict[str, set[str]] | None = None
    ) -> None:
        self.entries = [
            replace(e, aliases=tuple(a for a in e.aliases if _meaningful(a)))
            for e in entries
            if _meaningful(e.name)
        ]
        self.blocked = blocked or {}

    def snapshot(self) -> DictionarySnapshot:
        return DictionarySnapshot(sorted(self.entries, key=lambda e: e.name))

    def add(self, entries: list[NewEntry]) -> list[NewEntry]:
        operations = []
        for proposal in entries:
            if not _meaningful(proposal.name):
                continue
            snapshot = self.snapshot()
            existing = snapshot.resolve(proposal.name)
            name = (
                existing.name
                if existing
                else unicodedata.normalize("NFC", proposal.name)
            )
            # Do not promote another entry's occupied alias into a new name.
            if existing and _key(name) != _key(proposal.name):
                continue
            aliases = sorted(
                {
                    unicodedata.normalize("NFC", a)
                    for a in proposal.aliases
                    if _meaningful(a)
                    and _key(a) != _key(name)
                    and not snapshot.resolve(a)
                    and _key(a) not in self.blocked.get(_key(name), set())
                }
            )
            if existing:
                if not aliases:
                    continue
                index = self.entries.index(existing)
                self.entries[index] = replace(
                    existing,
                    aliases=tuple(sorted(set(existing.aliases) | set(aliases))),
                )
            else:
                self.entries.append(
                    ContextEntry(name, proposal.kind, tuple(aliases), source="learn")
                )
            operations.append(
                NewEntry(
                    name=name,
                    kind=existing.kind if existing else proposal.kind,
                    aliases=aliases,
                )
            )
        return operations


def _removed_aliases(root: Path) -> dict[str, set[str]]:
    path = root / "context.sqlite3"
    if not path.exists():
        return {}
    with closing(_read(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='edited'").fetchone():
            return {}
        return {
            name: {_key(a) for a in json.loads(payload).get("removed", [])}
            for name, payload in db.execute("SELECT name, payload FROM edited")
        }


def _wiki_entries(root: Path, errors: list[str]) -> list[NewEntry]:
    result = []
    directory = root / "pages"
    if directory.is_symlink():
        errors.append("wiki pages: symbolic-link directory excluded")
        return []
    for path in sorted(directory.glob("*.md")):
        if path.is_symlink() or not _meaningful(path.stem):
            continue
        try:
            # Bounded metadata read: bodies, query and Digest pages never enter
            # bootstrap, and a malformed/oversized page cannot abort a source.
            with path.open("rb") as stream:
                data = stream.read(8193)
            if len(data) > 8192:
                frontmatter_end = data.find(b"\n---", 4)
                if data.startswith(b"---\n") and frontmatter_end < 0:
                    raise ValueError("oversized frontmatter")
                data = data[: frontmatter_end + 5] if frontmatter_end >= 0 else b""
            properties = _properties(data)
            kind = {
                "operation": "area",
                "reference": "topic",
                "project": "project",
                "person": "person",
                "organization": "organization",
            }.get(str(properties.get("page_role")), properties.get("kind", "topic"))
            aliases = properties.get("aliases", [])
            result.append(
                NewEntry.model_validate(
                    {
                        "name": unicodedata.normalize("NFC", path.stem),
                        "kind": kind
                        if kind
                        in ("person", "organization", "project", "area", "topic")
                        else "topic",
                        "aliases": [
                            a for a in aliases if isinstance(a, str) and _meaningful(a)
                        ][:20]
                        if isinstance(aliases, list)
                        else [],
                    }
                )
            )
        except (OSError, ValueError, UnicodeError) as error:
            errors.append(f"wiki pages/{path.name}: {type(error).__name__}")
    return result


def _bootstrap(
    llm: LlmClient,
    memory: MemoryDictionary,
    threads: list[Thread],
    wiki: Path,
    errors: list[str],
    changes: Bootstrap | None = None,
    notices: list[str] | None = None,
) -> list[NewEntry]:
    pages = _wiki_entries(wiki, errors)
    tags = sorted({tag for t in threads for tag in t.tags if _meaningful(tag)})
    payload: dict[str, list[object]] = {"tags": [], "wiki_pages": [], "dictionary": []}
    count = 0

    # One shared count/UTF-8 byte cap includes JSON framing and all hints.
    def take(section: str, values: list[object]) -> None:
        nonlocal count
        for value in values:
            if count >= BOOTSTRAP_MAX_ENTRIES:
                break
            payload[section].append(value)
            if (
                len(json.dumps(payload, ensure_ascii=False).encode())
                > BOOTSTRAP_MAX_BYTES
            ):
                payload[section].pop()
                continue
            count += 1

    take("tags", list(tags))
    take("wiki_pages", [p.model_dump() for p in pages])
    take(
        "dictionary",
        [
            {
                "name": e.name,
                "kind": e.kind,
                "aliases": e.aliases,
                "source": e.source,
                "human_edited": e.human_edited,
            }
            for e in memory.snapshot().entries
        ],
    )
    if not count:
        return []
    response = llm.generate_json(
        "loom.reextract_bootstrap",
        (
            "기존 태그를 프로젝트·사람·조직·주제의 짧은 표준 이름 사전으로 정리한다."
            " area는 지속 책임 영역이며 topic은 PARA resource다. 영역은 적고 넓게 유지한다."
            " 새 project는 만들지 않는다. Wiki project는 사람이 편집하기 전까지 태그 후보가 아니다."
            " TODO/확인 같은 상태는 버린다. wiki 이름과 사람의 편집은 유지한다."
            " aliases는 실제 다른 호칭(약칭·영문/한글 표기·별명)만이다."
            " 오타·오인식·띄어쓰기·대소문자 변형은 aliases에 넣지 않고"
            " tag_corrections의 observed → canonical로만 매핑한다."
            " 학습된 미편집 사람·조직 이름의 직함·소속·메모를 떼고"
            " renames의 observed → canonical에 깨끗한 이름을 제안한다."
            " 기존 학습 표기는 녹취 교정으로만 보존하며 태그 별칭으로 만들지 않는다."
            " 정리한 결과가 이름이 아니면 entries와 renames에서 제외한다."
            " entries, renames, tag_corrections를 반환한다."
        ),
        json.dumps(payload, ensure_ascii=False),
        Bootstrap,
    )
    changes = changes if changes is not None else Bootstrap()
    rejected = 0
    for change in response.renames:
        old = memory.snapshot().resolve(change.observed)
        if (
            not old
            or old.name != change.observed
            or old.source != "learn"
            or old.human_edited
            or old.kind not in ("person", "organization")
            or not clean_name(change.canonical, old.kind)
            or memory.snapshot().resolve(change.canonical)
        ):
            rejected += 1
            continue
        memory.entries[memory.entries.index(old)] = replace(
            old,
            name=change.canonical,
            aliases=tuple(a for a in old.aliases if _key(a) != _key(old.name)),
            misrecognitions=(*old.misrecognitions, old.name),
        )
        changes.renames.append(change)
        changes.tag_corrections.append(change)
    excluded = [
        e
        for e in memory.entries
        if e.source == "learn"
        and not e.human_edited
        and e.kind in ("person", "organization")
        and not clean_name(e.name, e.kind)
    ]
    memory.entries = [e for e in memory.entries if e not in excluded]
    if excluded and notices is not None:
        notices.append(f"이름이 아닌 학습 항목 제외: {len(excluded)}")
    for change in response.tag_corrections:
        canonical = memory.snapshot().resolve(change.canonical)
        if change.observed in tags and (
            canonical or any(e.name == change.canonical for e in response.entries)
        ):
            changes.tag_corrections.append(change)
    if rejected and notices is not None:
        notices.append(f"이름 정리 제안 제외: {rejected}")
    # Keep page names even if the model omits them, but never invent a renamed
    # page to displace the canonical title. Only used proposals are later kept.
    proposals = {
        p.name: p
        for p in pages
        if p.kind != "project"
        or (
            (existing_page := memory.snapshot().resolve(p.name))
            and existing_page.human_edited
        )
    }
    for candidate in response.entries:
        if candidate.kind == "project":
            project = memory.snapshot().resolve(candidate.name)
            if not project or (project.source == "wiki" and not project.human_edited):
                if notices is not None:
                    notices.append("미승격 프로젝트 제안 제외: 1")
                continue
        if candidate.name in proposals:
            page = proposals[candidate.name]
            page.aliases = list(dict.fromkeys([*page.aliases, *candidate.aliases]))[:20]
        elif candidate.kind not in ("person", "organization") or clean_name(
            candidate.name, candidate.kind
        ):
            proposals[candidate.name] = candidate
        elif notices is not None:
            notices.append("이름이 아닌 사전 제안 제외: 1")
    # A spelling explicitly classified as an error must never become an alias.
    observed = {_key(c.observed) for c in changes.tag_corrections}
    for candidate in proposals.values():
        candidate.aliases = [a for a in candidate.aliases if _key(a) not in observed]
    return memory.add(list(proposals.values()))


def _active(threads: list[Thread]) -> list[Thread]:
    retracted = {t.retracts for t in threads if t.retracts}
    return [
        t
        for t in threads
        if t.thread_id not in retracted
        and t.retracts is None
        and t.kind not in ("거절", "실행", "태그정정")
    ]


def _external_refs(
    threads: list[Thread], source_id: str, old: list[Thread]
) -> list[int]:
    targets = {t.thread_id for t in old}
    retracted = {t.retracts for t in threads if t.retracts}
    found = []
    for t in threads:
        if t.thread_id in retracted or t.thread_id in targets:
            continue
        if t.source_id == source_id and t.actor != "human":
            continue
        refs = {
            t.resolves,
            t.blocked_by,
            t.updates,
            t.executes,
            t.retracts,
            t.correction.target if t.correction else None,
        }
        if targets & refs:
            found.append(t.seq)
    return sorted(found)


class ExtractionView:
    def __init__(self, threads: list[Thread], source_id: str) -> None:
        self.threads = [
            t.model_copy(update={"tags": [tag for tag in t.tags if _meaningful(tag)]})
            for t in threads
            if not (t.kind in ("일정", "요청") and t.source_id == source_id)
        ]

    def entries(self, query: ThreadQuery) -> list[Thread]:
        return [t for t in self.threads if t.kind != "실행"]

    def append(self, threads: list[ThreadDraft]) -> list[Thread]:
        raise RuntimeError("extraction view is read-only")


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


def _replace(
    plan: SourcePlan, drafts: list[ThreadDraft], now: datetime, baseline_ids: set[str]
) -> None:
    ids = {t.thread_id: _id(f"{plan.key}:new:{i}") for i, t in enumerate(drafts)}
    old_schedules = [t for t in plan.old if t.kind == "일정" and t.value is not None]
    superseded = {t.updates for t in old_schedules if t.updates}
    # Mapping/effects describe standing events, while plan.old still retracts
    # every historical active fact in the same source's update chains.
    old_schedules = [t for t in old_schedules if t.thread_id not in superseded]
    available = old_schedules.copy()
    for draft in drafts:
        data = draft.model_dump()
        data["thread_id"] = ids[draft.thread_id]
        data["tags"] = [t for t in draft.tags if _meaningful(t)]
        data["updates"] = None  # LLM may not select historical event lineages.
        data["retracts"] = None
        for field in ("blocked_by", "resolves"):
            data[field] = ids.get(data[field], data[field])
        if draft.kind == "일정" and draft.value is not None:
            value = ScheduleValue.model_validate(draft.value)
            equal = next(
                (
                    t
                    for t in available
                    if (v := ScheduleValue.model_validate(t.value)).start == value.start
                    and v.end == value.end
                ),
                None,
            )
            # Only equal slots preserve a known event automatically. A changed
            # legacy slot is linked solely to inherit baseline suppression and
            # is explicitly listed for manual calendar reconciliation.
            match = equal or next(
                (t for t in available if t.thread_id in baseline_ids), None
            )
            if match is None:
                match = next(
                    (t for t in old_schedules if t.thread_id in baseline_ids), None
                )
            effect, detail = "register", ""
            shown_old = match
            if match:
                data["updates"] = match.thread_id
                if match in available:
                    available.remove(match)
                old_value = ScheduleValue.model_validate(match.value)
                same_slot = bool(equal) and match.content == draft.content
                effect = "유지" if same_slot else "update"
                if same_slot and (old_value.title, old_value.description) != (
                    value.title,
                    value.description,
                ):
                    effect, detail = "update", "제목·설명 갱신"
                if match.thread_id in baseline_ids:
                    effect = "유지" if equal else "수동 확인"
                    if effect == "유지":
                        detail = ""
            elif available:
                shown_old = available.pop(0)
                effect = "remove+register"
            plan.calendar_effects.append(
                CalendarEffect(
                    old_thread_id=shown_old.thread_id if shown_old else None,
                    new_thread_id=data["thread_id"],
                    effect=effect,
                    detail="캘린더 수동 확인 필요: baseline 이전 일정의 시각 변경"
                    if effect == "수동 확인"
                    else detail,
                )
            )
        plan.new.append(ThreadDraft.model_validate(data))
    for old in available:
        plan.calendar_effects.append(
            CalendarEffect(
                old_thread_id=old.thread_id,
                effect="수동 확인" if old.thread_id in baseline_ids else "remove",
                detail="캘린더 수동 확인 필요: baseline 이전 일정"
                if old.thread_id in baseline_ids
                else "",
            )
        )
    plan.batch = [
        _aux(f"{plan.key}:retract:{t.thread_id}", t, now) for t in plan.old
    ] + plan.new


def _calendar_summary(
    root: Path, threads: list[Thread], report: Report, baseline: int
) -> None:
    history: dict[str, list[ActionRecord]] = {}
    path = root / "action-outbox.sqlite3"
    if path.exists():
        with closing(_read(path)) as db:
            for row in db.execute("SELECT * FROM actions ORDER BY lineage, position"):
                record = _record(row)
                history.setdefault(record.lineage, []).append(record)
    projected = threads.copy()
    high = max((t.seq for t in threads), default=0)
    for plan in report.sources:
        if plan.done or plan.skipped or plan.error:
            continue
        for draft in plan.batch:
            high += 1
            projected.append(Thread(**draft.model_dump(), seq=high))
    needed = derive_calendar_actions(
        [t for t in projected if t.kind != "실행"],
        lambda root: history.get(root, []),
        baseline=baseline,
        executions={t.thread_id: t for t in projected if t.kind == "실행"},
    )
    for plan in report.sources:
        for effect in plan.calendar_effects:
            if effect.effect == "수동 확인":
                continue
            action = next(
                (a for a in needed if a.thread_id == effect.new_thread_id), None
            )
            if action:
                if action.kind == "calendar.update":
                    effect.effect = "update"
                elif action.kind == "calendar.register":
                    effect.effect = (
                        "remove+register"
                        if any(
                            a.kind == "calendar.remove"
                            and a.source_id == plan.source_id
                            for a in needed
                        )
                        else "register"
                    )
            elif effect.new_thread_id:
                effect.effect = "유지"
            elif effect.effect == "remove" and not any(
                a.kind == "calendar.remove" and a.thread_id == effect.old_thread_id
                for a in needed
            ):
                effect.effect = "유지"


def _saved(root: Path, key: str) -> SourcePlan | None:
    path = root / "reextract.sqlite3"
    if not path.exists():
        return None
    with closing(_read(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='plans'").fetchone():
            return None
        row = db.execute(
            "SELECT payload, done FROM plans WHERE key=?", (key,)
        ).fetchone()
    if not row:
        return None
    plan = SourcePlan.model_validate_json(row[0])
    plan.done = bool(row[1])
    return plan


def _reviews(
    root: Path, report: Report, source: str | None
) -> tuple[
    dict[str, tuple[ReviewPackage, str, Verdict, Prepared]], dict[str, set[str]]
]:
    selected: dict[str, tuple[ReviewPackage, str, Verdict, Prepared]] = {}
    old_ids: dict[str, set[str]] = {}
    try:
        with closing(_read(root / "summary.sqlite3")) as db:
            rows = db.execute(
                "SELECT p.id, p.package, p.script, r.verdict, r.prepared FROM packages p JOIN reviews r ON p.id=r.id AND p.revision=r.revision WHERE r.stage='APPLIED' ORDER BY p.revision DESC, p.id"
            ).fetchall()
    except (OSError, sqlite3.Error):
        report.errors.append("summary.sqlite3: missing or unreadable; 다시 계획 필요")
        report.valid = False
        return selected, old_ids
    broken = set()
    for package_id, raw, script, raw_verdict, raw_prepared in rows:
        source_id = f"package:{package_id}"
        try:
            raw_package = json.loads(raw)
            source_id = str(raw_package.get("source_id", source_id))
            if source and source_id != source:
                continue
            if json.loads(raw_verdict).get("action") not in ("approve", "edit"):
                continue
            package = ReviewPackage.model_validate(raw_package)
            verdict = Verdict.model_validate_json(raw_verdict)
            if (package.package_id, package.revision) != (
                verdict.package_id,
                verdict.revision,
            ):
                raise ValueError("review revision mismatch")
            prepared = Prepared.model_validate_json(raw_prepared)
            old_ids.setdefault(source_id, set()).update(
                t.thread_id for t in prepared.threads
            )
            selected.setdefault(source_id, (package, script, verdict, prepared))
        except (ValueError, TypeError, AttributeError) as error:
            if source_id.startswith("package:"):
                # A malformed latest package must never silently select an
                # older revision of the same source. Recover provenance from
                # its already journaled extraction, without trusting its text.
                try:
                    prepared = Prepared.model_validate_json(raw_prepared)
                    origins = {t.source_id for t in prepared.threads}
                    if prepared.summary:
                        origins.add(prepared.summary.source_id)
                    if len(origins) == 1:
                        source_id = origins.pop()
                    else:
                        report.valid = False
                except ValueError:
                    report.valid = False
            broken.add(source_id)
            report.sources.append(
                SourcePlan(
                    source_id=source_id,
                    key=f"{RUN_KEY}:invalid:{package_id}",
                    error=type(error).__name__,
                )
            )
    for source_id in broken:
        selected.pop(source_id, None)
    return selected, old_ids


def reextract(
    root: Path,
    wiki: Path,
    llm: LlmClient,
    *,
    apply: bool = False,
    source: str | None = None,
    timezone: str = "Asia/Seoul",
    default_sphere: str = "회사",
    resume: Report | None = None,
    limit: int | None = None,
    daily_cap: int | None = None,
) -> Report:
    """Plan a migration.

    ``limit`` is the maximum number of new LLM sources planned in this run;
    ``daily_cap`` is the day's LLM call budget (bootstrap included), of which
    same-day calls carried in ``resume`` are already spent.
    """
    if apply:
        raise ValueError(
            "--apply --plan PATH 필요: 먼저 미리보기를 저장하고 다시 계획하십시오"
        )
    report = Report(state_root=str(root.resolve()))
    try:
        report.fingerprints = _signatures(root)
        if resume is not None and (
            resume.fingerprints != report.fingerprints or resume.run_key != RUN_KEY
        ):
            raise ValueError(
                "상태가 바뀌었습니다: --resume 없이 다시 계획"
                " (계획과 적용이 끝날 때까지 daemon을 계속 중지해야 합니다)"
            )
        threads = SnapshotFabric.open_readonly(root / "fabric.sqlite3").entries(
            ThreadQuery(include_executions=True)
        )
        path = root / "context.sqlite3"
        entries = (
            SnapshotDictionary.open_readonly(path).snapshot().list_entries()
            if path.exists()
            else []
        )
        blocked = _removed_aliases(root)
    except (OSError, ValueError, sqlite3.Error) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            "상태가 바뀌었습니다"
        ):
            raise
        reason = (
            str(error)
            if isinstance(error, ValueError) and "다시 계획" in str(error)
            else type(error).__name__
        )
        report.errors.append(f"state: {reason}")
        report.valid = False
        return _seal(report)
    selected, extracted_ids = _reviews(root, report, source)
    try:
        wiki_origins = wiki_source_ids(
            WikiStore.open_readonly(wiki.parent, root, wiki_root=wiki.name),
            {t.thread_id: t.source_id for t in threads},
        )
    except Exception:
        # Broken Wiki metadata is unknown lineage, not a crash: stay fail closed.
        wiki_origins = {UNKNOWN_WIKI_SOURCE}
    llm = ScopedClient(
        llm, {*selected, *(t.source_id for t in threads)} | wiki_origins, shared=True
    )
    if not report.valid:
        return _seal(report)
    active = _active(threads)
    broken = {p.source_id for p in report.sources if p.error}
    sources = sorted(
        (
            set(selected)
            | {
                t.source_id
                for t in active
                if t.actor == "human" and (source is None or t.source_id == source)
            }
        )
        - broken
    )
    reusable = {p.key: p for p in resume.sources if not p.error} if resume else {}
    reused: set[str] = set()
    pending = []
    for source_id in sources:
        revision = selected[source_id][0] if source_id in selected else None
        key = f"{RUN_KEY}:{source_id}:{revision.package_id + ':' + str(revision.revision) if revision else 'human'}"
        old = (
            [
                t
                for t in active
                if t.source_id == source_id
                and (
                    t.actor == "ai_approved"
                    or t.thread_id in extracted_ids.get(source_id, set())
                )
            ]
            if revision
            else []
        )
        try:
            plan = _saved(root, key)
            if plan is None and key in reusable:
                plan = reusable[key].model_copy(deep=True)
                reused.add(key)
            plan = plan or SourcePlan(source_id=source_id, key=key, old=old)
        except (ValueError, TypeError, OSError, sqlite3.Error) as error:
            report.sources.append(
                SourcePlan(
                    source_id=source_id,
                    key=key,
                    error=f"journal: {type(error).__name__}",
                )
            )
            continue
        guard_targets = (
            [
                t
                for t in threads
                if t.source_id == source_id
                and (
                    t.actor == "ai_approved"
                    or t.thread_id in extracted_ids.get(source_id, set())
                )
                and t.kind not in ("실행", "태그정정", "거절")
                and t.retracts is None
            ]
            if revision
            else []
        )
        refs = _external_refs(threads, source_id, guard_targets)
        if refs and not plan.done:
            plan.skipped, plan.referencing_seqs = "외부 참조로 건너뜀", refs
            plan.batch, plan.new, plan.additions = [], [], []
        report.sources.append(plan)
        if not plan.done and not plan.skipped and not plan.batch and key not in reused:
            pending.append(plan)
    today = datetime.now(ZoneInfo(timezone)).date().isoformat()
    report.llm_calls_by_day = dict(resume.llm_calls_by_day) if resume else {}
    if resume:
        report.notices.extend(n for n in resume.notices if n not in report.notices)
    report.today = today
    spent_today = report.llm_calls_by_day.get(today, 0)
    if limit is not None or daily_cap is not None:
        slots = limit if limit is not None else len(pending)
        if daily_cap is not None:
            # One daily call is reserved for the bootstrap request.
            slots = min(slots, max(0, daily_cap - spent_today - 1))
        deferred = []
        kept = []
        for plan in pending:
            if plan.source_id not in selected:
                kept.append(plan)  # human-only sources never call the LLM
            elif slots > 0:
                slots -= 1
                kept.append(plan)
            else:
                deferred.append(plan)
        pending = kept
        report.pending_sources = [p.source_id for p in deferred]
        report.sources = [p for p in report.sources if p not in deferred]
    report.llm_calls_by_day[today] = spent_today + sum(
        p.source_id in selected for p in pending
    )
    if pending:
        counter = _CountingClient(llm)
        changes = Bootstrap()
        try:
            bootstrap = _bootstrap(
                counter,
                MemoryDictionary(entries.copy(), blocked),
                threads,
                wiki,
                report.errors,
                changes,
                report.notices,
            )
        except Exception as error:
            report.llm_calls_by_day[today] += counter.calls
            for plan in pending:
                plan.error = f"bootstrap: {type(error).__name__}"
            return _seal(report)
        report.llm_calls_by_day[today] += counter.calls
        baseline = 0
        outbox_path = root / "action-outbox.sqlite3"
        if outbox_path.exists():
            try:
                with closing(_read(outbox_path)) as db:
                    row = db.execute(
                        "SELECT value FROM meta WHERE key='baseline'"
                    ).fetchone()
                    baseline = int(row[0]) if row else 0
            except (ValueError, OSError, sqlite3.Error) as error:
                report.errors.append(f"Action baseline: {type(error).__name__}")
                report.valid = False
                return _seal(report)
        for plan in pending:
            try:
                now = datetime.now(UTC)
                memory = MemoryDictionary(entries.copy(), blocked)
                plan.renames = list(changes.renames)
                plan.tag_corrections = list(changes.tag_corrections)
                for change in plan.renames:
                    prior_entry = memory.snapshot().resolve(change.observed)
                    if prior_entry:
                        memory.entries[memory.entries.index(prior_entry)] = replace(
                            prior_entry, name=change.canonical
                        )
                memory.entries = [
                    e
                    for e in memory.entries
                    if not (
                        e.source == "learn"
                        and not e.human_edited
                        and e.kind in ("person", "organization")
                        and not clean_name(e.name, e.kind)
                    )
                ]
                plan.additions = memory.add(bootstrap)
                if plan.source_id in selected:
                    package, script, verdict, original = selected[plan.source_id]
                    approved_at = original.applied_at
                    if approved_at is None and original.summary is not None:
                        approved_at = original.summary.created_at
                    reference = read_source_reference(root, package)
                    plan.source_time_estimated = reference.is_fallback
                    prepared = ApprovalExtractor(
                        llm,
                        memory,
                        ExtractionView(threads, plan.source_id),
                        timezone=timezone,
                        default_sphere=default_sphere,
                        approved_at=approved_at,
                        allow_current_approval_time=False,
                        source_occurred_at=reference.occurred_at,
                        source_time_is_fallback=reference.is_fallback,
                        contact_name=read_contact_name(
                            root, package.package_id, package.source_id
                        ),
                    ).prepare(package, script, verdict, now=now)
                    plan.additions.extend(memory.add(prepared.new_entries))
                    # prepared.corrections intentionally do not teach/learn.
                    _replace(
                        plan,
                        prepared.threads,
                        now,
                        baseline_schedule_ids(threads, baseline),
                    )
                    renamed = {
                        t.thread_id: _id(f"{plan.key}:new:{i}")
                        for i, t in enumerate(prepared.threads)
                    }
                    plan.location_proposals = [
                        draft.model_copy(
                            update={
                                "thread_ids": [
                                    renamed.get(i, i) for i in draft.thread_ids
                                ]
                            }
                        )
                        for draft in prepared.location_proposals
                    ]
                old_ids = {t.thread_id for t in plan.old}
                for thread in active:
                    if (
                        thread.source_id != plan.source_id
                        or thread.actor != "human"
                        or thread.thread_id in old_ids
                    ):
                        continue
                    mapping = {
                        _key(c.observed): c.canonical for c in plan.tag_corrections
                    }
                    tags = list(
                        dict.fromkeys(
                            (
                                e.name
                                if (
                                    e := memory.snapshot().resolve(
                                        mapping.get(_key(tag), tag)
                                    )
                                )
                                else tag
                            )
                            for tag in thread.tags
                            if _meaningful(tag)
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
                            _aux(
                                f"{plan.key}:tags:{thread.thread_id}",
                                thread,
                                now,
                                tags=tags,
                            )
                        )
                used = (
                    {tag for t in plan.new for tag in t.tags}
                    | {t.counterpart for t in plan.new if t.counterpart}
                    | {tag for change in plan.human_changes for tag in change.new_tags}
                )
                plan.additions = [e for e in plan.additions if e.name in used]
                plan.renames = [c for c in plan.renames if c.canonical in used]
                plan.tag_corrections = [
                    c for c in plan.tag_corrections if c.canonical in used
                ]
            except Exception as error:
                plan.error = (
                    str(error)
                    if isinstance(error, MissingApprovalTime)
                    else type(error).__name__
                )
                plan.batch = []
        global_names = MemoryDictionary(entries.copy(), blocked)
        # Reused plans replay their additions first, in their original order.
        for plan in sorted(report.sources, key=lambda p: p.key not in reused):
            if plan.error or plan.skipped or plan.done:
                continue
            for change in plan.renames:
                prior_entry = global_names.snapshot().resolve(change.observed)
                if prior_entry:
                    global_names.entries[global_names.entries.index(prior_entry)] = (
                        replace(prior_entry, name=change.canonical)
                    )
            safe = []
            for entry in plan.additions:
                snapshot = global_names.snapshot()
                owner = snapshot.resolve(entry.name)
                if owner and owner.name != entry.name:
                    plan.error = "dictionary proposal collision: 다시 계획 필요"
                    plan.batch = []
                    break
                aliases = [
                    alias
                    for alias in entry.aliases
                    if not (occupied := snapshot.resolve(alias))
                    or occupied.name == entry.name
                ]
                safe.append(entry.model_copy(update={"aliases": aliases}))
                global_names.add([safe[-1]])
            if not plan.error:
                plan.additions = safe
        used_names = {
            e.name for p in report.sources if not p.error for e in p.additions
        }
        carried = resume.bootstrap if resume else []
        report.bootstrap = [
            e
            for e in [
                *carried,
                *(b for b in bootstrap if b.name not in {c.name for c in carried}),
            ]
            if e.name in used_names
        ]
        try:
            _calendar_summary(root, threads, report, baseline)
        except (ValueError, OSError, sqlite3.Error) as error:
            report.errors.append(f"Action preview: {type(error).__name__}")
            report.valid = False
    report.unclassified = len(_unclassified_ids(report))
    try:
        if _signatures(root) != report.fingerprints:
            report.errors.append("계획 중 상태 변경: 다시 계획 필요")
            report.valid = False
    except (ValueError, OSError, sqlite3.Error) as error:
        report.errors.append(
            f"계획 중 상태 변경: {type(error).__name__}; 다시 계획 필요"
        )
        report.valid = False
    return _seal(report)


def save_plan(path: Path, root: Path, report: Report) -> None:
    if path.resolve().is_relative_to(root.resolve()):
        raise ValueError("--plan-out must be outside state_root")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(report.model_dump_json(indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_plan(path: Path) -> Report:
    report = Report.model_validate_json(path.read_bytes())
    if report.run_key != RUN_KEY or report.plan_id != _digest(
        report.model_dump(mode="json", exclude={"plan_id"})
    ):
        raise ValueError("계획 형식/무결성 오류: 다시 계획 필요")
    return report


def _journal_db(root: Path) -> sqlite3.Connection:
    path = root / "reextract.sqlite3"
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    db = sqlite3.connect(path)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA fullfsync=ON")
    db.execute(
        "CREATE TABLE IF NOT EXISTS plans (key TEXT PRIMARY KEY, payload TEXT NOT NULL, done INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL)"
    )
    return db


def _apply(
    root: Path,
    fabric: FabricStore,
    plan: SourcePlan,
    *,
    archive_root: Path | None = None,
) -> None:
    with closing(_journal_db(root)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO plans VALUES (?, ?, 0)",
            (plan.key, plan.model_dump_json()),
        )
    # Dictionary operations precede the atomic Fabric batch, so no committed
    # fact can reference a dictionary entry that has not yet been persisted.
    context = ContextDictionary(root, archive_root=archive_root)
    for change in plan.renames:
        if context.resolve(change.canonical) is None:
            context.rename(
                change.observed,
                change.canonical,
                human_edited=False,
                source_ids=(plan.source_id,),
            )
    for entry in plan.additions:
        actual = context.add_entry(
            entry.name,
            entry.kind,
            tuple(entry.aliases),
            human_edited=False,
            drop_conflicting_aliases=True,
            source_ids=(plan.source_id,),
        )
        for alias in entry.aliases:
            context.add_alias(
                actual.name, alias, human_edited=False, source_ids=(plan.source_id,)
            )
    fabric.append(plan.batch)
    with closing(_journal_db(root)) as db, db:
        db.execute("UPDATE plans SET done=1 WHERE key=?", (plan.key,))
    plan.done = True


def _propose(root: Path, report: Report, default_sphere: str) -> None:
    """Location proposals and one summary, written only after every Fabric append."""
    if any(p.error for p in report.sources) or any(
        not p.done and not p.skipped for p in report.sources
    ):
        return
    proposals = LocationProposals(root)
    now = datetime.now(UTC)
    for plan in report.sources:
        if plan.skipped:
            continue
        for draft in plan.location_proposals:
            proposals.propose(**draft.model_dump(), now=now)
    ids = _unclassified_ids(report)
    if ids:
        proposals.propose(
            kind="unclassified",
            reason="reextract_unclassified",
            sphere=default_sphere,
            path=None,
            fallback=f"{default_sphere}/{UNCLASSIFIED}",
            thread_ids=ids,
            source_ids=[p.source_id for p in report.sources if not p.skipped],
            key=report.plan_id,
            now=now,
        )


def apply_report(
    root: Path,
    supplied: Report,
    *,
    archive_root: Path | None = None,
    default_sphere: str = "회사",
) -> Report:
    """No model dependency; caller holds InstanceLock for validation and apply."""
    if (
        not supplied.valid
        or supplied.state_root != str(root.resolve())
        or not supplied.fingerprints
    ):
        raise ValueError("유효한 계획이 없습니다: 다시 계획 필요")
    if supplied.pending_sources:
        raise ValueError("계획이 끝나지 않았습니다: --resume으로 이어서 계획")
    current = _signatures(root)
    expected = supplied.fingerprints
    path = root / "reextract.sqlite3"
    if path.exists():
        with closing(_read(path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():
                row = db.execute(
                    "SELECT checkpoint FROM runs WHERE id=?", (supplied.plan_id,)
                ).fetchone()
                if row:
                    expected = json.loads(row[0])
    if current != expected:
        raise ValueError("Fabric/사전/승인/Action 상태가 바뀌었습니다: 다시 계획 필요")
    report = supplied.model_copy(deep=True)
    report.applied = True
    if not any(not p.done and not p.skipped and not p.error for p in report.sources):
        _propose(root, report, default_sphere)
        return report
    with closing(_journal_db(root)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO runs VALUES (?, ?)",
            (report.plan_id, json.dumps(current)),
        )
    fabric = FabricStore(root / "fabric.sqlite3")
    for plan in report.sources:
        if plan.done or plan.skipped or plan.error:
            continue
        saved = _saved(root, plan.key)
        if saved and saved.done:
            plan.done = True
            continue
        if saved and saved.batch != plan.batch:
            plan.error = "journal conflict: --discard-plan 후 다시 계획 필요"
            continue
        try:
            _apply(root, fabric, plan, archive_root=archive_root)
        except Exception as error:
            plan.error = type(error).__name__
        # A normal failure records its exact resulting state for safe retries.
        # A process death between writes/checkpoint deliberately fails closed;
        # re-planning retains the original prepared batch from the journal.
        with closing(_journal_db(root)) as db, db:
            db.execute(
                "UPDATE runs SET checkpoint=? WHERE id=?",
                (json.dumps(_signatures(root)), report.plan_id),
            )
    _propose(root, report, default_sphere)
    return report


def discard_plan(root: Path, source: str | None = None) -> int:
    path = root / "reextract.sqlite3"
    if not path.exists():
        return 0
    existing = {
        t.thread_id
        for t in SnapshotFabric.open_readonly(root / "fabric.sqlite3").entries(
            ThreadQuery(include_executions=True)
        )
    }
    count = 0
    with closing(_journal_db(root)) as db, db:
        rows = db.execute("SELECT key, payload FROM plans WHERE done=0").fetchall()
        for key, payload in rows:
            plan = SourcePlan.model_validate_json(payload)
            if source and plan.source_id != source:
                continue
            # Already appended atomic batches become completed tombstones;
            # discarding never retracts facts or permits conflicting IDs.
            if any(t.thread_id in existing for t in plan.batch):
                db.execute("UPDATE plans SET done=1 WHERE key=?", (key,))
            else:
                db.execute("DELETE FROM plans WHERE key=?", (key,))
            count += 1
        db.execute("DELETE FROM runs")
    return count


def _today_calls(report: Report) -> int:
    """Calls of the run's own day in the configured timezone."""
    days = report.llm_calls_by_day
    if report.today:
        return days.get(report.today, 0)
    return days[max(days)] if days else 0


def render(report: Report) -> str:
    lines = [
        "적용" if report.applied else "미리보기 (Weaver 상태·사전·원장 쓰기 없음)",
        "초기 사전 항목: "
        + json.dumps([e.model_dump() for e in report.bootstrap], ensure_ascii=False),
    ]
    lines.extend(f"오류: {error}" for error in report.errors)
    lines.extend(f"알림: {notice}" for notice in report.notices)
    for plan in report.sources:
        lines.append(f"출처 {plan.source_id}" + (" (반영됨)" if plan.done else ""))
        if plan.source_time_estimated:
            lines.append("  출처 시각 추정")
        if plan.skipped:
            lines.append(f"  {plan.skipped}: 참조 seq={plan.referencing_seqs}")
            continue
        for label, items in (("기존 Threads", plan.old), ("새 Threads", plan.new)):
            lines.append(label + (" →" if label == "기존 Threads" else ""))
            for t in items:
                lines.append(
                    f"  {t.kind}: {t.content} | tags={t.tags} | counterpart={t.counterpart} | occurred_at={t.occurred_at.isoformat()} | recorded_at={t.recorded_at.isoformat()} | {t.field}={json.dumps(t.value, ensure_ascii=False)}"
                )
        for change in plan.renames:
            lines.append(f"  자동 이름 정리: {change.observed} → {change.canonical}")
        for change in plan.tag_corrections:
            lines.append(f"  태그 매핑: {change.observed} → {change.canonical}")
        for human_change in plan.human_changes:
            lines.append(
                f"  사람 작성 {human_change.thread_id}: {human_change.old_tags} → {human_change.new_tags}"
            )
        for effect in plan.calendar_effects:
            lines.append(
                f"  캘린더 {effect.old_thread_id} → {effect.new_thread_id}: {effect.effect} {effect.detail}"
            )
        lines.append(
            "새 사전 항목/별칭: "
            + json.dumps([e.model_dump() for e in plan.additions], ensure_ascii=False)
        )
        if plan.error:
            lines.append(f"  오류: {plan.error}")
    lines.append(
        f"미분류 {report.unclassified}건 · 남은 원본 {len(report.pending_sources)}개"
        f" · 오늘 LLM {_today_calls(report)}회"
        f" · 캘린더 update {sum(e.effect == 'update' for p in report.sources for e in p.calendar_effects)}건"
    )
    return "\n".join(lines)
