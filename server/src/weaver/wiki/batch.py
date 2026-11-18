"""Compile bounded evidence into selected, protected Wiki sections."""

import json
import logging
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import UTC, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, JsonValue, WithJsonSchema

from weaver.fabric import ThreadQuery
from weaver.fabric.state import project
from weaver.llm.errors import SchemaMismatch
from weaver.schema import Thread
from weaver.wiki.compiler import Compiler
from weaver.wiki.digest import DigestBuilder, DigestEntry
from weaver.wiki.document import MEMO, sections, without_memo
from weaver.wiki.generation import fingerprint
from weaver.wiki.links import link_label, summary_path
from weaver.wiki.promotion import (
    PROJECT_THREAD_PREFIX,
    ProjectLifecycle,
    PromotionShape,
)
from weaver.wiki.prompts import BATCH_PROMPT, RULES_VERSION, WORK_VAULT_RULES
from weaver.wiki.selection import (
    MAX_NEW_THREADS,
    MAX_RELATED_THREADS,
    MAX_SOURCE_CHARS,
    EvidenceIndex,
    SectionAssignments,
    affected,
    assignment_map,
    section_map,
)
from weaver.wiki.snapshot import canonical_threads
from weaver.wiki.store import HumanEditPending

logger = logging.getLogger(__name__)


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


_EVIDENCE_HEADING = "\n## 근거\n"
# Bump when validation rules change so recorded failures are retried once.
_VALIDATOR_VERSION = 2


def _strip_managed_evidence(body: str) -> str:
    """Drop the code-managed trailing evidence list, keeping any text after it.

    Only the last evidence heading and the "- " lines right below it are
    removed, and only when no other section follows; anything a person wrote
    after the list stays in the body.
    """
    parts = sections(body)
    evidence = next((part for part in parts if part.key == "## 근거"), None)
    if evidence is None:
        return body
    # Only a generated trailing list is removable. An intervening ordinary
    # section remains a validation error rather than silently losing content.
    tail = parts[parts.index(evidence) + 1 :]
    if any(part.key != MEMO for part in tail):
        return body
    return "".join(part.text for part in parts if part is not evidence)


def _current_states(threads: list[Thread], active: list[Thread]) -> dict[str, object]:
    states: dict[str, object] = {}
    for tag in sorted({tag for t in active for tag in t.tags}):
        state = project(tag, threads)
        states[tag] = {
            "fields": {
                k: {"value": v.value, "thread_id": v.thread_id}
                for k, v in state.fields.items()
            },
            "assignees": sorted(state.assignees),
            "open_requests": [
                t.thread_id
                for t in state.open_requests
                if t.thread_id in {item.thread_id for item in active}
            ],
        }
    return states


class WikiPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=100)
    role: Literal["project", "operation", "reference", "person", "organization"]
    summary: str = Field(min_length=1, max_length=500)
    thread_ids: list[str] = Field(min_length=1)
    state_tag: str | None
    aliases: list[str]
    body: str = Field(min_length=1, max_length=24000)
    section_threads: SectionAssignments = Field(default_factory=list)


class ExcludedThread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    reason: str = Field(min_length=1, max_length=500)


# Keep the provider schema structured while preserving malformed JSON items for
# isolated validation after pages/digests have been accepted.
type PromotionItem = Annotated[
    JsonValue, WithJsonSchema(PromotionShape.model_json_schema())
]


class WikiBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[WikiPage] = Field(min_length=1, max_length=64)
    excluded_threads: list[ExcludedThread]
    digests: list[DigestEntry] = Field(default_factory=list)
    promotions: list[PromotionItem] = Field(default_factory=list)


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
    """One bounded LLM call; validate every section before writing pages."""

    digest: DigestBuilder | None = None
    timezone: ZoneInfo = ZoneInfo("UTC")

    def _existing_pages(
        self, scope: dict[str, set[str]] | None = None
    ) -> dict[str, tuple[dict[str, JsonValue], str, str]]:
        pages = {}
        for path in sorted((self.store.root / "pages").glob("*.md")):
            relative = _nfc(path.relative_to(self.store.root).as_posix())
            if scope is not None and relative not in scope:
                continue
            props, body, digest = self.store.read(relative)
            if not self.store.is_tracked_version(relative, digest):
                raise HumanEditPending(relative)
            pages[relative] = (props, body, digest)
        return pages

    def _protected_spans(
        self, path: str, scope: dict[str, set[str]] | None
    ) -> list[JsonValue]:
        spans = self.store.metadata(path).get("human_spans", [])
        if not isinstance(spans, list):
            return []
        return [
            span
            for span in spans
            if isinstance(span, dict)
            and (scope is None or span.get("section") in scope.get(path, set()))
        ]

    def _tables(self) -> None:
        with closing(sqlite3.connect(self.state_path)) as db, db:
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
        lifecycle = ProjectLifecycle(
            self.state_path, self.context, self.fabric, self.timezone
        )
        now = datetime.fromtimestamp(self.store.clock(), UTC)
        lifecycle.refresh(now)
        threads = canonical_threads(self.fabric.entries(ThreadQuery()))
        bookmark = self.bookmark()
        new = sorted(
            (
                t
                for t in threads
                if t.seq > bookmark
                and not t.thread_id.startswith(PROJECT_THREAD_PREFIX)
            ),
            key=lambda t: t.seq,
        )[:MAX_NEW_THREADS]
        high = max(
            (t.seq for t in new if not t.thread_id.startswith(PROJECT_THREAD_PREFIX)),
            default=0,
        )
        if high <= self.bookmark():
            self._sync_pages()
            return
        threads = [
            t
            for t in threads
            if t.seq <= high or t.thread_id.startswith(PROJECT_THREAD_PREFIX)
        ]
        retracted = {t.retracts for t in threads if t.retracts}
        active = [
            t
            for t in threads
            if t.thread_id not in retracted
            and not t.retracts
            and t.kind not in ("태그정정", "거절", "실행")
        ]
        all_threads = threads
        index = EvidenceIndex(self.state_path, active)
        scope: dict[str, set[str]] | None = None
        if bookmark:
            scope = {}
            for page_path in sorted((self.store.root / "pages").glob("*.md")):
                path = _nfc(page_path.relative_to(self.store.root).as_posix())
                meta = self.store.metadata(path)
                chosen = affected(meta, new, index)
                if chosen:
                    scope[path] = chosen
        existing = self._existing_pages(scope)
        selected_ids = {t.thread_id for t in new}
        for path, chosen in (scope or {}).items():
            mapping = self.store.metadata(path).get("sections", {})
            if isinstance(mapping, dict):
                for key in chosen:
                    data = mapping.get(key, {})
                    if isinstance(data, dict):
                        ids = data.get("thread_ids", [])
                        if isinstance(ids, list):
                            selected_ids.update(str(key) for key in ids)
        # A bounded initial window; subsequent requests carry just affected facts.
        active = sorted(
            (t for t in active if t.thread_id in selected_ids and t.seq <= high),
            key=lambda t: t.seq,
            reverse=True,
        )[:MAX_RELATED_THREADS]
        active.sort(key=lambda t: t.seq)
        if not active:
            # Retractions still need their original evidence as context.
            targets = {
                t.retracts or (t.correction.target if t.correction else None)
                for t in new
            }
            active = [t for t in all_threads if t.thread_id in targets]
        if not active:
            with closing(sqlite3.connect(self.state_path)) as db, db:
                db.execute("UPDATE bookmark SET seq=?", (high,))
            return
        sources = {}
        source_count = len({t.source_id for t in active})
        source_budget = min(MAX_SOURCE_CHARS, 32000 // max(1, source_count))
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
                "body": summary.body[:source_budget],
            }
        stable_threads = lifecycle.stable_threads(threads)
        digest_targets = self.digest.plan(stable_threads, high) if self.digest else []
        bounded_digests = [target.payload() for target in digest_targets]
        for payload_target, target in zip(bounded_digests, digest_targets, strict=True):
            # Period payloads can contain many old facts; evidence and the current
            # bounded change set suffice for incremental digest updates.
            payload_target["inputs"] = {
                "threads": [
                    t.model_dump(mode="json")
                    for t in active
                    if t.source_id in target.evidence
                ]
            }
        corpus = {
            "rules_version": RULES_VERSION,
            "changes": [t.model_dump(mode="json") for t in new],
            "withdrawn_ids": sorted(
                key for key in retracted if key and key in selected_ids
            ),
            "dictionary": [
                {
                    "name": e.name,
                    "kind": e.kind,
                    "archived": e.archived,
                    "promoted": e.promoted,
                }
                for e in self.context.snapshot().entries
                if e.name in {tag for t in active for tag in t.tags}
                or e.name in {t.counterpart for t in active}
            ],
            "threads": [t.model_dump(mode="json") for t in active],
            "sources": list(sources.values()),
            "current_states": _current_states(threads, active),
        }
        active_ids = {t.thread_id for t in active}
        stable_active = [t for t in stable_threads if t.thread_id in active_ids]
        snapshot = fingerprint(
            {
                "corpus": {
                    "rules_version": RULES_VERSION,
                    "threads": [t.model_dump(mode="json") for t in stable_active],
                    "sources": list(sources.values()),
                    "current_states": _current_states(stable_threads, stable_active),
                },
                "high": high,
                "prompt": BATCH_PROMPT,
            }
        )
        failure_key = fingerprint(
            {
                "snapshot": snapshot,
                "existing": {p: v[2] for p, v in existing.items()},
                "validator": _VALIDATOR_VERSION,
            }
        )
        with closing(sqlite3.connect(self.state_path)) as db, db:
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
                "digest_targets": bounded_digests,
                "section_scope": {
                    path: sorted(keys) for path, keys in (scope or {}).items()
                },
                "existing_pages": [
                    # The evidence section is written by code; the model must
                    # not see it, or it echoes it back as page content.
                    {
                        "path": path,
                        "body": (
                            "# " + path[6:-3] + "\n\n"
                            if scope is not None and "__intro__" not in scope[path]
                            else ""
                        )
                        + "".join(
                            section.text
                            for section in sections(
                                _strip_managed_evidence(without_memo(body))
                            )
                            if scope is None or section.key in scope[path]
                        ),
                        "properties": props,
                        "protected_human_text": self._protected_spans(path, scope),
                    }
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
                self._validate(result, active, sources, existing, scope)
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
                with closing(sqlite3.connect(self.state_path)) as db, db:
                    db.execute(
                        "INSERT OR REPLACE INTO batch_failures VALUES (?, ?)",
                        (failure_key, reason),
                    )
                logger.warning("wiki batch validation failed: %s", reason)
                raise
            completed = {}
            with closing(sqlite3.connect(self.state_path)) as db, db:
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
        self._validate(result, active, sources, existing, scope)
        by_id = {t.thread_id: t for t in active}
        for page in result.pages:
            path = f"pages/{page.title}.md"
            try:
                props, previous_body, base = self.store.read(path)
                props = self.store.metadata(path) | props
                if not self.store.is_tracked_version(path, base):
                    raise HumanEditPending(path)
            except FileNotFoundError:
                props, previous_body, base = {}, "", None
            if base is not None and completed.get(path) == base:
                continue
            selected = [by_id[key] for key in page.thread_ids]
            old_meta = self.store.metadata(path)
            if scope is not None and path in existing:
                replacements = {
                    section.key: section.text
                    for section in sections(page.body)
                    if section.key in scope[path]
                }
                if set(replacements) != scope[path]:
                    raise WikiValidationError(
                        "wiki partial result must include selected sections"
                    )
                page.body = "".join(
                    replacements.get(section.key, section.text)
                    for section in sections(_strip_managed_evidence(previous_body))
                )
                old_ids = old_meta.get("thread_ids", [])
                if isinstance(old_ids, list):
                    page.thread_ids = list(
                        dict.fromkeys(
                            [*(str(key) for key in old_ids), *page.thread_ids]
                        )
                    )
                all_by_id = {t.thread_id: t for t in all_threads}
                selected = [
                    all_by_id[key] for key in page.thread_ids if key in all_by_id
                ]
            elif (
                scope is not None
                and (self.store.root / path).exists()
                and path not in existing
            ):
                raise WikiValidationError(
                    "wiki partial result touches an unselected page"
                )
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
                    "kind": {"operation": "area", "reference": "topic"}.get(
                        page.role, page.role
                    ),
                    "page_role": page.role,
                    "state_tag": page.state_tag,
                    "aliases": list[JsonValue](page.aliases),
                    "summary": page.summary,
                    "thread_ids": list[JsonValue](dict.fromkeys(page.thread_ids)),
                }
            )
            evidence_sources = dict(sources)
            for source_id in sorted({t.source_id for t in selected} - sources.keys()):
                try:
                    summary = self.summary(source_id)
                except (ValueError, LookupError, FileNotFoundError):
                    continue
                evidence_sources[source_id] = {
                    "title": summary.title,
                    "path": summary_path(summary.title, source_id),
                }
            evidence = []
            for source_id in sorted(
                {t.source_id for t in selected if t.source_id in evidence_sources}
            ):
                source_path = str(evidence_sources[source_id]["path"])
                evidence.append(
                    f"- [[{source_path.removesuffix('.md')}|{link_label(evidence_sources[source_id]['title'])}]]"
                )
            human = sorted(
                t.seq for t in selected if t.source_id not in evidence_sources
            )
            if human:
                evidence.append(
                    "- Fabric 사람 기록: " + ", ".join(f"#{seq}" for seq in human)
                )
            body = page.body.rstrip() + "\n\n## 근거\n" + "\n".join(evidence) + "\n"
            written = self.store.write(path, props, body, base)
            new_mapping = section_map(
                self.store.read(path)[1], selected, assignment_map(page.section_threads)
            )
            if scope is not None and path in existing:
                old_mapping = old_meta.get("sections", {})
                if isinstance(old_mapping, dict):
                    new_mapping = old_mapping | {
                        key: value
                        for key, value in new_mapping.items()
                        if key in scope[path]
                    }
            self.store.write_metadata(
                path,
                {
                    "sections": json.loads(json.dumps(new_mapping)),
                    "source_ids": list[JsonValue](
                        sorted({t.source_id for t in selected})
                    ),
                },
            )
            with closing(sqlite3.connect(self.state_path)) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO compiled_pages VALUES (?, ?, ?)",
                    (path, snapshot, written),
                )
        if self.digest is not None:
            self.digest.apply(digest_targets, result.digests, high)
        self._sync_pages()
        lifecycle.apply(result.promotions, active, now, replay=False)
        with closing(sqlite3.connect(self.state_path)) as db, db:
            db.execute("UPDATE bookmark SET seq=?", (high,))
            db.execute(
                "DELETE FROM batches WHERE seq < (SELECT MAX(seq) FROM batches) AND seq NOT IN (SELECT seq FROM batches ORDER BY seq DESC LIMIT 2)"
            )

        lifecycle.refresh(now)

    def _validate(
        self,
        result: WikiBatch,
        active: list[Thread],
        sources: dict[str, dict[str, str]],
        existing: dict[str, tuple[dict[str, JsonValue], str, str]],
        scope: dict[str, set[str]] | None = None,
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
        known_paths = {
            _nfc(p.relative_to(self.store.root).as_posix()).removesuffix(".md")
            for p in (self.store.root / "pages").glob("*.md")
        }
        if scope is not None:
            for page in result.pages:
                path = f"pages/{page.title}.md"
                if path.removesuffix(".md") in known_paths and path not in existing:
                    raise WikiValidationError(
                        "wiki partial result touches an unselected page"
                    )
                if path in existing:
                    keys = {s.key for s in sections(page.body)} - {"__intro__"}
                    if not keys.issubset(scope[path]) or not scope[path].issubset(
                        {s.key for s in sections(page.body)}
                    ):
                        raise WikiValidationError(
                            "wiki partial result must include selected sections"
                        )
        if not set(existing).issubset({p + ".md" for p in page_paths}):
            raise WikiValidationError("wiki batch would discard an existing page")
        covered = set()
        for page in result.pages:
            if any(part.key == MEMO for part in sections(page.body)):
                raise WikiValidationError("wiki model must not write personal notes")
            path = f"pages/{page.title}.md"
            private = self.store.metadata(path)
            spans = private.get("human_spans", [])
            if isinstance(spans, list):
                for span in spans:
                    if isinstance(span, dict) and isinstance(span.get("text"), str):
                        if (
                            scope is None
                            or span.get("section") in scope.get(path, set())
                        ) and str(span["text"]).strip() not in page.body:
                            raise WikiValidationError(
                                "wiki partial result lost human wording"
                            )
            ids = set(page.thread_ids)
            for key, assigned in assignment_map(page.section_threads).items():
                if key not in {s.key for s in sections(page.body)} or not set(
                    assigned
                ).issubset(ids):
                    raise WikiValidationError("invalid wiki section evidence")
            if not ids.issubset(by_id):
                raise WikiValidationError("unknown wiki evidence thread")
            if page.state_tag and not any(
                page.state_tag in by_id[key].tags for key in ids
            ):
                raise WikiValidationError("wiki state tag is unrelated to the page")
            covered.update(ids)
            if not page.body.startswith(f"# {page.title}\n"):
                raise WikiValidationError("wiki page must start with its title")
            # A trailing echo of the managed section is dropped, not fatal;
            # code rewrites it from the page's evidence threads.
            page.body = _strip_managed_evidence(page.body)
            if page.body.startswith("---") or "\n## 근거" in page.body:
                raise WikiValidationError(
                    "wiki page must not duplicate managed metadata"
                )
            allowed = (
                page_paths
                | known_paths
                | {
                    str(sources[by_id[key].source_id]["path"]).removesuffix(".md")
                    for key in ids
                    if by_id[key].source_id in sources
                }
            )
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
        with closing(sqlite3.connect(self.state_path)) as db, db:
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
                (path[6:-3], self.store.metadata(path))
                for path in sorted(published)
                if path.startswith("pages/")
                and self.store.metadata(path).get("page_role") == role
            ]
            if pages:
                lines.extend([f"## {label}", ""])
                lines.extend(
                    f"- [[pages/{title}]] — {meta.get('summary', title)}"
                    for title, meta in pages
                )
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
            f"- 페이지: {sum(path.startswith('pages/') for path in published)}개",
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
