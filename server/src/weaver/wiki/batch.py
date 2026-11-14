"""Compile a complete bounded Fabric snapshot into a coherent work wiki."""

import json
import logging
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from weaver.fabric import ThreadQuery
from weaver.fabric.state import project
from weaver.llm.errors import SchemaMismatch
from weaver.schema import Thread
from weaver.wiki.compiler import Compiler
from weaver.wiki.digest import DigestBuilder, DigestEntry
from weaver.wiki.generation import fingerprint
from weaver.wiki.links import summary_path
from weaver.wiki.prompts import BATCH_PROMPT, RULES_VERSION, WORK_VAULT_RULES
from weaver.wiki.snapshot import canonical_threads
from weaver.wiki.store import HumanEditPending

logger = logging.getLogger(__name__)


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


class WikiPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=100)
    role: Literal["project", "operation", "reference", "person", "organization"]
    summary: str = Field(min_length=1, max_length=500)
    thread_ids: list[str] = Field(min_length=1)
    state_tag: str | None
    aliases: list[str]
    body: str = Field(min_length=1, max_length=24000)


class ExcludedThread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    reason: str = Field(min_length=1, max_length=500)


class WikiBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[WikiPage] = Field(min_length=1, max_length=64)
    excluded_threads: list[ExcludedThread]
    digests: list[DigestEntry] = Field(default_factory=list)


_VALIDATION_REASONS = frozenset(
    {
        "invalid or duplicate wiki page title",
        "wiki batch would discard an existing page",
        "unknown wiki evidence thread",
        "wiki state tag is unrelated to the page",
        "wiki page must start with its title",
        "wiki page must not duplicate managed metadata",
        "wiki batch must use page links without unverified anchors",
        "wiki batch contains an unknown link",
        "wiki page links must use validated wikilinks",
        "duplicate or assigned excluded thread",
        "wiki batch does not account for all active threads",
    }
)


class WikiValidationError(ValueError):
    """A fixed, content-free batch validation reason."""


class BatchCompiler(Compiler):
    """One corpus-level LLM call; validate the whole batch before writing pages."""

    digest: DigestBuilder | None = None

    def _existing_pages(self) -> dict[str, tuple[dict[str, JsonValue], str, str]]:
        pages = {}
        for path in sorted((self.store.root / "pages").glob("*.md")):
            relative = _nfc(path.relative_to(self.store.root).as_posix())
            props, body, digest = self.store.read(relative)
            if not self.store.is_tracked_version(relative, digest):
                raise HumanEditPending(relative)
            pages[relative] = (props, body, digest)
        return pages

    def _tables(self) -> None:
        with sqlite3.connect(self.state_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS batches "
                "(seq INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, "
                "result TEXT NOT NULL, bases TEXT NOT NULL, created_at TEXT NOT NULL)"
            )

            db.execute(
                "CREATE TABLE IF NOT EXISTS batch_failures (fingerprint TEXT PRIMARY KEY, reason TEXT NOT NULL)"
            )

    def _run(self) -> None:
        self._tables()
        self.store.restore_readonly()
        for edit in self.store.scan(self.store.clock()):
            self._edit(edit)
        threads = canonical_threads(self.fabric.entries(ThreadQuery()))
        high = max((t.seq for t in threads), default=0)
        if high <= self.bookmark():
            self._sync_pages()
            return
        retracted = {t.retracts for t in threads if t.retracts}
        active = [
            t
            for t in threads
            if t.thread_id not in retracted
            and not t.retracts
            and t.kind not in ("태그정정", "거절", "실행")
        ]
        if not active:
            # There is no new knowledge to publish, but existing pages may now
            # contain withdrawn claims: do not silently mark them up to date.
            raise ValueError("wiki snapshot has no active evidence")
        existing = self._existing_pages()
        sources = {}
        for source_id in sorted({t.source_id for t in active}):
            try:
                summary = self.summary(source_id)
            except (ValueError, LookupError, FileNotFoundError):
                if any(
                    t.source_id == source_id and t.actor == "ai_approved"
                    for t in active
                ):
                    raise
                continue
            path = summary_path(summary.title, source_id)
            try:
                self.store.read(path)
            except FileNotFoundError:
                self.store.write(path, {}, summary.body, None, readonly=True)
            sources[source_id] = {
                "source_id": source_id,
                "title": summary.title,
                "path": path,
                "body": summary.body,
            }
        states = {}
        for tag in sorted({tag for t in active for tag in t.tags}):
            state = project(tag, threads)
            states[tag] = {
                "fields": {
                    k: {"value": v.value, "thread_id": v.thread_id}
                    for k, v in state.fields.items()
                },
                "assignees": sorted(state.assignees),
                "open_requests": [t.thread_id for t in state.open_requests],
            }
        digest_targets = self.digest.plan(threads, high) if self.digest else []
        corpus = {
            "rules_version": RULES_VERSION,
            "threads": [t.model_dump(mode="json") for t in active],
            "sources": list(sources.values()),
            "current_states": states,
        }
        snapshot = fingerprint({"corpus": corpus, "high": high, "prompt": BATCH_PROMPT})
        failure_key = fingerprint(
            {"snapshot": snapshot, "existing": {p: v[2] for p, v in existing.items()}}
        )
        with sqlite3.connect(self.state_path) as db:
            failed = db.execute(
                "SELECT reason FROM batch_failures WHERE fingerprint=?", (failure_key,)
            ).fetchone()
            row = db.execute(
                "SELECT result, bases FROM batches WHERE seq=? AND fingerprint=?",
                (high, snapshot),
            ).fetchone()
            completed = dict(
                db.execute(
                    "SELECT path, hash FROM compiled_pages WHERE fingerprint=?",
                    (snapshot,),
                )
            )
        bases = json.loads(row[1]) if row is not None else {}
        cached_targets = bases.get("__digest_targets__", {})
        reusable = (
            row is not None
            and all(
                cached_targets.get(target.path) == target.payload()
                for target in digest_targets
            )
            and all(
                item[2] in (bases.get(path), completed.get(path))
                for path, item in existing.items()
            )
        )
        if reusable:
            assert row is not None
            result = WikiBatch.model_validate_json(row[0])
        else:
            if failed:
                logger.warning("wiki batch validation skipped: %s", failed[0])
                return
            for path, (_, _, digest) in existing.items():
                self.store.check_write(path, digest)
            payload = corpus | {
                "digest_targets": [target.payload() for target in digest_targets],
                "existing_pages": [
                    {"path": path, "body": body, "properties": props}
                    for path, (props, body, _) in existing.items()
                ],
            }
            try:
                result = self.llm.generate_json(
                    "wiki.compile_batch",
                    BATCH_PROMPT,
                    json.dumps(payload, ensure_ascii=False),
                    WikiBatch,
                )
                self._validate(result, active, sources, existing)
            except (ValueError, SchemaMismatch) as error:
                reason = (
                    "schema_mismatch"
                    if isinstance(error, SchemaMismatch)
                    else "invalid_batch"
                )
                if (
                    isinstance(error, WikiValidationError)
                    and str(error) in _VALIDATION_REASONS
                ):
                    reason += ": " + str(error)
                with sqlite3.connect(self.state_path) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO batch_failures VALUES (?, ?)",
                        (failure_key, reason),
                    )
                logger.warning("wiki batch validation failed: %s", reason)
                raise
            completed = {}
            with sqlite3.connect(self.state_path) as db:
                db.execute(
                    "DELETE FROM compiled_pages WHERE fingerprint=?", (snapshot,)
                )
                db.execute(
                    "INSERT OR REPLACE INTO batches VALUES (?, ?, ?, ?, ?)",
                    (
                        high,
                        snapshot,
                        result.model_dump_json(),
                        json.dumps(
                            {p: item[2] for p, item in existing.items()}
                            | {
                                "__digest_targets__": {
                                    target.path: target.payload()
                                    for target in digest_targets
                                }
                            }
                        ),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        self._validate(result, active, sources, existing)
        by_id = {t.thread_id: t for t in active}
        for page in result.pages:
            path = f"pages/{page.title}.md"
            try:
                props, _, base = self.store.read(path)
                if not self.store.is_tracked_version(path, base):
                    raise HumanEditPending(path)
            except FileNotFoundError:
                props, base = {}, None
            if base is not None and completed.get(path) == base:
                continue
            selected = [by_id[key] for key in page.thread_ids]
            page_state = project(page.state_tag, threads) if page.state_tag else None
            for field in ("deadline", "schedule", "status"):
                props[field] = (
                    page_state.fields[field].value
                    if page_state and field in page_state.fields
                    else None
                )
            props.update(
                {
                    "assignees": list[JsonValue](sorted(page_state.assignees))
                    if page_state
                    else [],
                    "kind": page.role
                    if page.role in ("project", "person", "organization")
                    else "topic",
                    "page_role": page.role,
                    "state_tag": page.state_tag,
                    "aliases": list[JsonValue](page.aliases),
                    "summary": page.summary,
                    "thread_ids": list[JsonValue](dict.fromkeys(page.thread_ids)),
                }
            )
            evidence = []
            for source_id in sorted(
                {t.source_id for t in selected if t.source_id in sources}
            ):
                source_path = str(sources[source_id]["path"])
                evidence.append(f"- [[{source_path.removesuffix('.md')}]]")
            human = sorted(t.seq for t in selected if t.source_id not in sources)
            if human:
                evidence.append(
                    "- Fabric 사람 기록: " + ", ".join(f"#{seq}" for seq in human)
                )
            body = page.body.rstrip() + "\n\n## 근거\n" + "\n".join(evidence) + "\n"
            written = self.store.write(path, props, body, base)
            with sqlite3.connect(self.state_path) as db:
                db.execute(
                    "INSERT OR REPLACE INTO compiled_pages VALUES (?, ?, ?)",
                    (path, snapshot, written),
                )
        if self.digest is not None:
            self.digest.apply(digest_targets, result.digests, high)
        self._sync_pages()
        with sqlite3.connect(self.state_path) as db:
            db.execute("UPDATE bookmark SET seq=?", (high,))
            db.execute(
                "DELETE FROM batches WHERE seq < (SELECT MAX(seq) FROM batches) AND seq NOT IN (SELECT seq FROM batches ORDER BY seq DESC LIMIT 2)"
            )

    def _validate(
        self,
        result: WikiBatch,
        active: list[Thread],
        sources: dict[str, dict[str, str]],
        existing: dict[str, tuple[dict[str, JsonValue], str, str]],
    ) -> None:
        by_id = {t.thread_id: t for t in active}
        titles = set()
        for page in result.pages:
            page.title = _nfc(page.title).strip()
            page.state_tag = _nfc(page.state_tag) if page.state_tag else None
            page.aliases = list(dict.fromkeys(_nfc(a).strip() for a in page.aliases))
            if (
                page.title.casefold() in titles
                or page.title.startswith(".")
                or re.search(r"[/\\#^|\[\]\x00-\x1f\x7f]", page.title)
                or page.title.casefold() in {"todo", "확인", "진행", "완료"}
                or any(not a for a in page.aliases)
            ):
                raise WikiValidationError("invalid or duplicate wiki page title")
            titles.add(page.title.casefold())
        page_paths = {f"pages/{p.title}" for p in result.pages}
        if not set(existing).issubset({p + ".md" for p in page_paths}):
            raise WikiValidationError("wiki batch would discard an existing page")
        covered = set()
        for page in result.pages:
            ids = set(page.thread_ids)
            if not ids.issubset(by_id):
                raise WikiValidationError("unknown wiki evidence thread")
            if page.state_tag and not any(
                page.state_tag in by_id[key].tags for key in ids
            ):
                raise WikiValidationError("wiki state tag is unrelated to the page")
            covered.update(ids)
            if not page.body.startswith(f"# {page.title}\n"):
                raise WikiValidationError("wiki page must start with its title")
            if page.body.startswith("---") or "\n## 근거" in page.body:
                raise WikiValidationError(
                    "wiki page must not duplicate managed metadata"
                )
            allowed = page_paths | {
                str(sources[by_id[key].source_id]["path"]).removesuffix(".md")
                for key in ids
                if by_id[key].source_id in sources
            }
            for link in re.findall(r"\[\[([^\]\n]+)\]\]", page.body):
                if "#" in link.split("|", 1)[0]:
                    raise WikiValidationError(
                        "wiki batch must use page links without unverified anchors"
                    )
                target = _nfc(link.split("|", 1)[0].split("#", 1)[0]).removesuffix(
                    ".md"
                )
                if target not in allowed:
                    raise WikiValidationError("wiki batch contains an unknown link")
            if re.search(r"\]\([^)]*\.md(?:[)#])", page.body):
                raise WikiValidationError(
                    "wiki page links must use validated wikilinks"
                )
        excluded = [x.thread_id for x in result.excluded_threads]
        if len(excluded) != len(set(excluded)) or set(excluded) & covered:
            raise WikiValidationError("duplicate or assigned excluded thread")
        if covered | set(excluded) != set(by_id):
            raise WikiValidationError(
                "wiki batch does not account for all active threads"
            )

    def refresh_index(self) -> None:
        """Publish navigation from actual files, never from guessed link targets."""
        self._tables()
        with sqlite3.connect(self.state_path) as db:
            row = db.execute(
                "SELECT result, seq, created_at FROM batches ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return
        result = WikiBatch.model_validate_json(row[0])
        published = {path for path, _, _ in self.store.published_pages()}
        lines = [
            "# Weaver Wiki",
            "",
            "Fabric에서 종합한 업무 문서입니다. 날짜별 기록은 Digest에서 확인합니다.",
            "",
        ]
        for role, label in [
            ("project", "프로젝트"),
            ("operation", "운영"),
            ("reference", "기술 지식"),
            ("person", "사람"),
            ("organization", "조직"),
        ]:
            pages = [
                p
                for p in result.pages
                if p.role == role and f"pages/{p.title}.md" in published
            ]
            if pages:
                lines.extend([f"## {label}", ""])
                lines.extend(f"- [[pages/{p.title}]] — {p.summary}" for p in pages)
                lines.append("")
        sections: list[tuple[str, list[str]]] = [
            (
                "업무 조회",
                [
                    p
                    for p in ("열린 요청.md", "다가오는 마감.md", "사람별 연락.md")
                    if p in published
                ],
            ),
            ("Digest", sorted(p for p in published if p.startswith("Digest/"))),
            ("원본 요약", sorted(p for p in published if p.startswith("summaries/"))),
        ]
        for label, paths in sections:
            if paths:
                lines.extend([f"## {label}", ""])
                lines.extend(f"- [[{p.removesuffix('.md')}]]" for p in paths)
                lines.append("")
        lines.extend(
            ["## 운영", "", "- [[schema|Wiki 운영 규칙]]", "- [[log|생성 기록]]", ""]
        )
        log = [
            "# Wiki 생성 기록",
            "",
            f"- 생성 시각: {row[2]}",
            f"- Fabric 책갈피: seq {row[1]}",
            f"- 운영 규칙: {RULES_VERSION}",
            f"- 페이지: {len(result.pages)}개",
            "",
            "## 별도 페이지에 반영하지 않은 기록",
            "",
        ]
        log.extend(
            f"- `{item.thread_id}`: {item.reason}" for item in result.excluded_threads
        )
        if not result.excluded_threads:
            log.append("- 없음")
        for path, body in [
            ("schema.md", "# " + WORK_VAULT_RULES),
            ("log.md", "\n".join(log) + "\n"),
            ("index.md", "\n".join(lines)),
        ]:
            try:
                _, previous, base = self.store.read(path)
                if previous == body and self.store.is_tracked_version(path, base):
                    continue
            except FileNotFoundError:
                base = None
            self.store.write(path, {}, body, base, readonly=True)
