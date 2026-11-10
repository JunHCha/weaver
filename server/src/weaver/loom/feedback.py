"""Private feedback records, alias registry updates and structured prompt examples.

`state_root/feedback.sqlite3` is 0600, outside sync/vault, FULL/fullfsync.
One row per event_id=(proposal_id, rev); a retry with the same fingerprint is a
no-op and a different payload for the same key is an error. Only edited paths
keep before/after values; the original snapshot stays in the review journal and
is referenced by (proposal_id, rev). `purge` removes raw fragments (before/after,
excerpts, instructions) after the retention period and keeps structural metadata.
Errors and logs never include record content.
"""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

from weaver.config import FeedbackSettings, ServerConfig
from weaver.schema import Proposal
from weaver.schema.internal import EntityIndex
from weaver.schema.models import EntityType, RejectReason, RelationType, ThreadKind

from . import entities
from .propose import FeedbackEvent
from .review_store import ReviewStore

SCHEMA_VERSION = 1
# Closed vocabularies and IDs are not raw text; they may appear in fake samples.
_PUBLIC = {
    value
    for alias in (EntityType, ThreadKind, RelationType, RejectReason)
    for value in get_args(alias.__value__)
}
_ID = re.compile(r"(?:ent|evt|prp|src)_[0-9A-HJKMNP-TV-Z]{26}")


def is_raw_text(value: str) -> bool:
    """False for closed vocabularies, durable IDs and JSON literals (null, 1...)."""
    if value in _PUBLIC or _ID.fullmatch(value):
        return False
    try:
        return isinstance(json.loads(value), str | list | dict)
    except (ValueError, RecursionError):
        return True


EXAMPLES_VERSION = "loom.examples.v1"
# Only closed vocabularies may leave the device as prompt examples.
_EXAMPLE_KINDS = {
    "entities.entity_id",
    "entities.type",
    "entry_drafts.kind",
    "relations.type",
}
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS events (
        proposal_id TEXT NOT NULL, rev INTEGER NOT NULL,
        source_id TEXT NOT NULL, verdict TEXT NOT NULL,
        reviewed_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        reject_reason TEXT, instruction TEXT,
        outcome TEXT NOT NULL CHECK(outcome IN ('entries','revised','rejected')),
        result TEXT NOT NULL, versions TEXT NOT NULL,
        fingerprint TEXT NOT NULL, purged_at TEXT,
        PRIMARY KEY(proposal_id, rev))""",
    """CREATE TABLE IF NOT EXISTS corrections (
        proposal_id TEXT NOT NULL, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
        kind TEXT NOT NULL, path TEXT NOT NULL,
        before TEXT, after TEXT, excerpt TEXT, entity_ids TEXT NOT NULL,
        PRIMARY KEY(proposal_id, rev, seq),
        FOREIGN KEY(proposal_id, rev) REFERENCES events(proposal_id, rev))""",
    """CREATE TABLE IF NOT EXISTS event_entities (
        proposal_id TEXT NOT NULL, rev INTEGER NOT NULL, entity_id TEXT NOT NULL,
        PRIMARY KEY(proposal_id, rev, entity_id))""",
    """CREATE TABLE IF NOT EXISTS example_uses (
        task TEXT NOT NULL, source_id TEXT NOT NULL, used_at TEXT NOT NULL,
        rule TEXT NOT NULL, selected TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS events_recorded ON events(recorded_at)",
    "CREATE INDEX IF NOT EXISTS entities_event ON event_entities(entity_id)",
    *entities.SCHEMA,
)


class FeedbackConflict(ValueError):
    """Same event_id with a different payload; content is never attached."""


def _parts(path: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]


def _resolve(document: Any, parts: list[str]) -> Any:
    for token in parts:
        document = (
            document[int(token)] if isinstance(document, list) else document[token]
        )
    return document


def _item_entities(group: str, item: dict[str, Any], ids: dict[str, str]) -> list[str]:
    if group == "entities":
        refs = [item["ref"]]
        direct = [item["entity_id"]] if item.get("entity_id") else []
    elif group == "relations":
        refs, direct = [item["from"], item["to"]], []
    else:
        refs, direct = list(item.get("entities", [])), []
    found = {*direct, *(ids.get(r, r) for r in refs)}
    return sorted(e for e in found if e.startswith("ent_"))


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SqliteFeedbackStore:
    """FeedbackSink implementation plus read APIs; record() runs under the review lock."""

    def __init__(
        self,
        path: Path,
        journal: ReviewStore,
        settings: FeedbackSettings | None = None,
    ) -> None:
        self.path = path.resolve()
        self.journal = journal
        self.settings = settings or FeedbackSettings()
        self.version = EXAMPLES_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._init_lock(), self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("unsupported feedback store version")
            for statement in _SCHEMA:
                db.execute(statement)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @classmethod
    def from_config(
        cls, config: ServerConfig, journal: ReviewStore
    ) -> "SqliteFeedbackStore":
        return cls(config.state_root / "feedback.sqlite3", journal, config.feedback)

    @contextmanager
    def _init_lock(self) -> Iterator[None]:
        lock = self.path.with_suffix(".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _secure(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm", "-journal"):
            with suppress(FileNotFoundError):
                os.chmod(f"{self.path}{suffix}", 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._secure()
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("PRAGMA checkpoint_fullfsync=ON")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    # -- FeedbackSink -----------------------------------------------------

    def record(self, event: FeedbackEvent) -> None:
        pid, rev = event.event_id
        verdict = event.verdict
        if (verdict.proposal_id, verdict.rev) != (pid, rev):
            raise ValueError("feedback event_id does not match verdict")
        result: Any
        if isinstance(event.result, Proposal):
            outcome, result = "revised", {"next_rev": event.result.rev}
        elif verdict.verdict == "reject":
            outcome, result = "rejected", []
        else:
            outcome, result = "entries", [e.entry_id for e in event.result]
        fingerprint = hashlib.sha256(
            _dump(
                {
                    "proposal": event.proposal.model_dump(mode="json"),
                    "verdict": verdict.model_dump(mode="json"),
                    "result": result,
                    "entity_ids": event.entity_ids,
                    "versions": event.versions,
                }
            ).encode()
        ).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT fingerprint FROM events WHERE proposal_id=? AND rev=?",
                (pid, rev),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise FeedbackConflict("conflicting feedback for event_id")
                return
            original = self.journal.proposal(pid, rev)
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    pid,
                    rev,
                    original.source.source_id,
                    verdict.verdict,
                    verdict.reviewed_at.isoformat(),
                    datetime.now(UTC).isoformat(),
                    verdict.reject_reason,
                    verdict.instruction,
                    outcome,
                    _dump(result),
                    _dump(event.versions),
                    fingerprint,
                ),
            )
            ids = dict(event.entity_ids)
            related = {e.entity_id for e in original.entities if e.entity_id}
            related.update(ids.values())
            db.executemany(
                "INSERT INTO event_entities VALUES (?,?,?)",
                [(pid, rev, e) for e in sorted(related)],
            )
            before_doc = original.model_dump(mode="json", by_alias=True)
            explicit_links: set[int] = set()
            alias_edits: set[int] = set()
            for seq, edit in enumerate(verdict.edits):
                parts = _parts(edit.path)
                if parts == ["summary"]:
                    kind, item = "summary", None
                else:
                    group, index, field = parts[0], int(parts[1]), parts[2]
                    kind = f"{group}.{field}"
                    item = before_doc[group][index]
                    if kind == "entities.entity_id" and edit.value is not None:
                        explicit_links.add(index)
                    if kind == "entities.aliases":
                        alias_edits.add(index)
                db.execute(
                    "INSERT INTO corrections VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        pid,
                        rev,
                        seq,
                        kind,
                        edit.path,
                        _dump(_resolve(before_doc, parts)),
                        _dump(edit.value),
                        item["thread_excerpt"] if item else None,
                        _dump(_item_entities(parts[0], item, ids) if item else []),
                    ),
                )
            if verdict.verdict in ("approve", "edit"):
                entities.apply_approval(
                    db,
                    original,
                    event.proposal,
                    ids,
                    explicit_links,
                    alias_edits,
                    (pid, rev),
                )

    # -- Reads ------------------------------------------------------------

    def event(self, proposal_id: str, rev: int) -> dict[str, Any] | None:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM events WHERE proposal_id=? AND rev=?", (proposal_id, rev)
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["result"] = json.loads(data["result"])
            data["versions"] = json.loads(data["versions"])
            data["corrections"] = [
                {
                    "kind": c["kind"],
                    "path": c["path"],
                    "before": None if c["before"] is None else json.loads(c["before"]),
                    "after": None if c["after"] is None else json.loads(c["after"]),
                    "excerpt": c["excerpt"],
                    "entity_ids": json.loads(c["entity_ids"]),
                }
                for c in db.execute(
                    "SELECT * FROM corrections WHERE proposal_id=? AND rev=? ORDER BY seq",
                    (proposal_id, rev),
                )
            ]
        return data

    def events(self) -> list[tuple[str, int]]:
        with self.connect() as db:
            return [
                (r[0], r[1])
                for r in db.execute(
                    "SELECT proposal_id,rev FROM events ORDER BY recorded_at, proposal_id, rev"
                )
            ]

    def index(self) -> EntityIndex:
        """Approved EntityIndex snapshot for LlmExtractor; conflicts excluded."""
        with self.connect() as db:
            return entities.build_index(db)

    def alias_conflicts(self) -> list[tuple[str, str]]:
        with self.connect() as db:
            return entities.conflicts(db)

    # -- Prompt examples (ExampleSource) ----------------------------------

    def select(
        self, task: str, source_id: str, entity_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Newest structured corrections for the given entities within the caps.

        Only loom.extract has examples: revise instructions are free text and
        never become examples. Selected (proposal_id, rev, seq) are recorded.
        """
        limit, budget = self.settings.example_limit, self.settings.example_bytes
        if task != "loom.extract" or not entity_ids or limit == 0:
            return []
        marks = ",".join("?" * len(entity_ids))
        kinds = ",".join("?" * len(_EXAMPLE_KINDS))
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT c.proposal_id, c.rev, c.seq, c.kind, c.before, c.after,
                       c.entity_ids, e.reviewed_at, e.recorded_at
                FROM corrections c JOIN events e USING(proposal_id, rev)
                WHERE e.purged_at IS NULL AND c.before IS NOT NULL
                  AND c.kind IN ({kinds})
                  AND EXISTS (SELECT 1 FROM json_each(c.entity_ids)
                              WHERE value IN ({marks}))
                ORDER BY e.reviewed_at DESC, e.recorded_at DESC, c.seq""",
                (*sorted(_EXAMPLE_KINDS), *sorted(entity_ids)),
            ).fetchall()
            rejects = db.execute(
                f"""SELECT e.proposal_id, e.rev, -1, 'reject', NULL, e.reject_reason,
                       json_group_array(x.entity_id), e.reviewed_at, e.recorded_at
                FROM events e JOIN event_entities x USING(proposal_id, rev)
                WHERE e.verdict='reject' AND x.entity_id IN ({marks})
                GROUP BY e.proposal_id, e.rev""",
                tuple(sorted(entity_ids)),
            ).fetchall()
            chosen: list[dict[str, Any]] = []
            keys: list[str] = []
            # Newest review first; within one review, edits in submitted order.
            ordered = sorted(
                [*rows, *rejects],
                key=lambda r: (_instant(r[7]), r[8], -r[2]),
                reverse=True,
            )
            for pid, rev, seq, kind, before, after, ids, _at, _rec in ordered:
                if len(chosen) >= limit:
                    break
                example: dict[str, Any] = {
                    "correction": kind,
                    "entities": sorted(set(json.loads(ids)) & entity_ids),
                }
                if kind == "reject":
                    example["reason"] = after
                else:
                    example["before"] = json.loads(before)
                    example["after"] = json.loads(after)
                if len(_dump([*chosen, example]).encode()) > budget:
                    continue
                chosen.append(example)
                keys.append(f"{pid}:{rev}:{seq}")
            if chosen:
                db.execute(
                    "INSERT INTO example_uses VALUES (?,?,?,?,?)",
                    (
                        task,
                        source_id,
                        datetime.now(UTC).isoformat(),
                        EXAMPLES_VERSION,
                        _dump(keys),
                    ),
                )
        return chosen

    def example_uses(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                {
                    "task": r[0],
                    "source_id": r[1],
                    "rule": r[2],
                    "selected": json.loads(r[3]),
                }
                for r in db.execute(
                    "SELECT task,source_id,rule,selected FROM example_uses ORDER BY rowid"
                )
            ]

    # -- Retention --------------------------------------------------------

    def purge(self, now: datetime | None = None) -> int:
        """Drop raw fragments older than retention_days; keep structure and keys."""
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=self.settings.retention_days)).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            targets = db.execute(
                "SELECT proposal_id,rev FROM events WHERE purged_at IS NULL AND recorded_at<?",
                (cutoff,),
            ).fetchall()
            for pid, rev in targets:
                db.execute(
                    "UPDATE corrections SET before=NULL, after=NULL, excerpt=NULL "
                    "WHERE proposal_id=? AND rev=?",
                    (pid, rev),
                )
                db.execute(
                    "UPDATE events SET instruction=NULL, purged_at=? "
                    "WHERE proposal_id=? AND rev=?",
                    (now.isoformat(), pid, rev),
                )
        return len(targets)

    def sensitive_strings(self, proposal_id: str, rev: int) -> set[str]:
        """Raw strings that a public fake sample must never contain."""
        found: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, str):
                if value.lstrip()[:1] in ("{", "[", '"'):
                    # JSON-encoded text: compare the decoded strings too.
                    with suppress(ValueError, RecursionError):
                        decoded = json.loads(value)
                        if decoded != value:
                            walk(decoded)
                if is_raw_text(value):
                    found.add(value)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)

        with suppress(ValueError):
            proposal = self.journal.proposal(proposal_id, rev)
            walk([proposal.summary, proposal.thread_excerpt])
            for entity in proposal.entities:
                walk([entity.name, entity.aliases, entity.thread_excerpt])
                if entity.ask:
                    walk([c.label for c in entity.ask.choices])
            for relation in proposal.relations:
                walk([relation.reason, relation.thread_excerpt])
            for draft in proposal.entry_drafts:
                walk([draft.value, draft.thread_excerpt])
                if draft.request:
                    walk(draft.request.content)
            for schedule in proposal.schedules:
                walk([schedule.title, schedule.thread_excerpt])
        record = self.event(proposal_id, rev)
        if record:
            walk(record["instruction"])
            for c in record["corrections"]:
                walk([c["before"], c["after"], c["excerpt"]])
        return found
