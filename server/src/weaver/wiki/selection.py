"""Private SQL tag/counterpart selection with FTS5 as a fallback, never a corpus prompt."""

import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict

from weaver.context import DictionarySnapshot
from weaver.schema import Thread
from weaver.wiki.document import MEMO, sections, text_hash

MAX_NEW_THREADS = 32
MAX_RELATED_THREADS = 64
MAX_SOURCE_CHARS = 12000


class SectionEvidence(BaseModel):
    """Explicit rows keep section assignments usable in strict provider schemas."""

    model_config = ConfigDict(extra="forbid")
    section: str
    thread_ids: list[str]


def _legacy_assignments(value: object) -> object:
    # Accept cached early responses while exposing only a typed list on the wire.
    if isinstance(value, dict):
        return [{"section": key, "thread_ids": ids} for key, ids in value.items()]
    return value


type SectionAssignments = Annotated[
    list[SectionEvidence], BeforeValidator(_legacy_assignments)
]


def assignment_map(values: SectionAssignments) -> dict[str, list[str]]:
    if len({row.section for row in values}) != len(values):
        raise ValueError("duplicate section evidence")
    return {row.section: row.thread_ids for row in values}


class EvidenceIndex:
    def __init__(
        self,
        path: Path,
        threads: list[Thread],
        dictionary: DictionarySnapshot | None = None,
    ) -> None:
        self.path = path
        self.areas = (
            {e.name for e in dictionary.entries if e.kind == "area"}
            if dictionary
            else {"업무 운영"}
        )
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DROP TABLE IF EXISTS wiki_evidence")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wiki_evidence (id TEXT PRIMARY KEY, counterpart TEXT);
                CREATE TABLE IF NOT EXISTS wiki_evidence_tags (id TEXT, tag TEXT, PRIMARY KEY (id, tag));
                CREATE INDEX IF NOT EXISTS wiki_evidence_tag ON wiki_evidence_tags(tag);
                CREATE VIRTUAL TABLE IF NOT EXISTS wiki_evidence_fts USING fts5(id UNINDEXED, content);
                DELETE FROM wiki_evidence; DELETE FROM wiki_evidence_tags; DELETE FROM wiki_evidence_fts;
            """)
            db.executemany(
                "INSERT INTO wiki_evidence VALUES (?, ?)",
                [(t.thread_id, t.counterpart) for t in threads],
            )
            db.executemany(
                "INSERT OR IGNORE INTO wiki_evidence_tags VALUES (?, ?)",
                [
                    (t.thread_id, tag)
                    for t in threads
                    for tag in t.tags
                    if tag not in self.areas
                ],
            )
            db.executemany(
                "INSERT INTO wiki_evidence_fts VALUES (?, ?)",
                [(t.thread_id, t.content or "") for t in threads],
            )

    def related(
        self, tags: list[str], counterparts: list[str], text: str = ""
    ) -> set[str]:
        ids: set[str] = set()
        with closing(sqlite3.connect(self.path)) as db:
            for tag in tags:
                if tag in self.areas:
                    continue
                ids.update(
                    row[0]
                    for row in db.execute(
                        "SELECT id FROM wiki_evidence_tags WHERE tag=? ORDER BY id",
                        (tag,),
                    )
                )
            for name in counterparts:
                ids.update(
                    row[0]
                    for row in db.execute(
                        "SELECT id FROM wiki_evidence WHERE counterpart=? ORDER BY id",
                        (name,),
                    )
                )
            # Lexical overlap only helps when exact business keys find nothing.
            if not ids:
                words = re.findall(r"\w{2,}", text)[:12]
                query = " OR ".join('"' + w + '"' for w in words)
                if query:
                    ids.update(
                        row[0]
                        for row in db.execute(
                            "SELECT id FROM wiki_evidence_fts WHERE wiki_evidence_fts MATCH ? ORDER BY rank LIMIT 8",
                            (query,),
                        )
                    )
        return ids


def section_map(
    body: str, threads: list[Thread], assignments: dict[str, list[str]] | None = None
) -> dict[str, object]:
    by_id = {t.thread_id: t for t in threads}
    result: dict[str, object] = {}
    for section in sections(body):
        if section.key in (MEMO, "## 근거"):
            continue
        ids = assignments.get(section.key, list(by_id)) if assignments else list(by_id)
        evidence = [by_id[key] for key in ids if key in by_id]
        result[section.key] = {
            "thread_ids": [t.thread_id for t in evidence],
            "source_ids": sorted({t.source_id for t in evidence}),
            "tags": sorted({tag for t in evidence for tag in t.tags}),
            "counterparts": sorted({t.counterpart for t in evidence if t.counterpart}),
            "hash": text_hash(section.text),
            "search_text": section.text[:1000],
        }
    return result


def affected(
    meta: Mapping[str, object], new: list[Thread], index: EvidenceIndex
) -> set[str]:
    mapping = meta.get("sections", {})
    if not isinstance(mapping, dict):
        return set()
    selected = set()
    for key, data in mapping.items():
        if not isinstance(data, dict):
            continue
        ids = set(data.get("thread_ids", []))
        related = index.related(data.get("tags", []), data.get("counterparts", []))
        if (
            (
                not (set(data.get("tags", [])) - index.areas)
                or any(
                    not (set(t.tags) - index.areas) and not t.counterpart for t in new
                )
            )
            and not data.get("counterparts")
            and not any(t.thread_id in related for t in new)
        ):
            related.update(index.related([], [], str(data.get("search_text", ""))))
        for thread in new:
            targets = {thread.thread_id, thread.retracts, thread.resolves}
            if thread.correction:
                targets.add(thread.correction.target)
            if ids & targets or thread.thread_id in related:
                selected.add(key)
    return selected
