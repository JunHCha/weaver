"""Minimal approved Entity/alias registry, the source of EntityIndex (#27).

Rows come only from approved review snapshots. Each alias has an origin:
`human` for an explicit correction (the mention name linked through an ask
choice, or an alias edit) and `ai` for aliases proposed with a newly approved
entity. An AI alias colliding with another entity's name or active alias is
stored as conflict. A human alias wins over a colliding AI alias, which is
demoted to conflict; it is a conflict itself only against another entity's name
or human alias. A new entity name demotes colliding AI aliases of other entities;
colliding human aliases stay active because the extractor keeps every collision
and asks. Conflicts are excluded from the index. Entities are never merged,
reject/revise never change the registry, and Relations are out of scope.
"""

import sqlite3

from weaver.schema import Proposal
from weaver.schema.internal import EntityIndex, IndexedEntity

from .extract import normalize_name

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS entities (
        entity_id TEXT PRIMARY KEY NOT NULL, type TEXT NOT NULL,
        name TEXT NOT NULL, proposal_id TEXT NOT NULL, rev INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS aliases (
        entity_id TEXT NOT NULL REFERENCES entities(entity_id),
        normalized TEXT NOT NULL, alias TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','conflict')),
        origin TEXT NOT NULL CHECK(origin IN ('ai','human')),
        proposal_id TEXT NOT NULL, rev INTEGER NOT NULL,
        PRIMARY KEY(entity_id, normalized))""",
    "CREATE INDEX IF NOT EXISTS aliases_normalized ON aliases(normalized, status)",
)


def _name_taken(db: sqlite3.Connection, entity_id: str, normalized: str) -> bool:
    return any(
        normalize_name(name) == normalized
        for (name,) in db.execute(
            "SELECT name FROM entities WHERE entity_id<>?", (entity_id,)
        )
    )


def _demote_ai(db: sqlite3.Connection, entity_id: str, normalized: str) -> None:
    db.execute(
        "UPDATE aliases SET status='conflict' WHERE normalized=? AND entity_id<>? "
        "AND origin='ai' AND status='active'",
        (normalized, entity_id),
    )


def _add_alias(
    db: sqlite3.Connection,
    entity_id: str,
    canonical: str,
    label: str,
    origin: str,
    event_id: tuple[str, int],
) -> None:
    normalized = normalize_name(label)
    if not normalized or normalized == normalize_name(canonical):
        return
    existing = db.execute(
        "SELECT origin FROM aliases WHERE entity_id=? AND normalized=?",
        (entity_id, normalized),
    ).fetchone()
    if existing and (existing[0] == "human" or origin == "ai"):
        return  # Idempotent; an AI proposal never downgrades a human alias.
    others = {
        row[0]
        for row in db.execute(
            "SELECT origin FROM aliases WHERE normalized=? AND entity_id<>? "
            "AND status='active'",
            (normalized, entity_id),
        )
    }
    if origin == "human":
        conflict = _name_taken(db, entity_id, normalized) or "human" in others
        if not conflict:
            _demote_ai(db, entity_id, normalized)
    else:
        conflict = _name_taken(db, entity_id, normalized) or bool(others)
    status = "conflict" if conflict else "active"
    pid, rev = event_id
    db.execute(
        "INSERT OR REPLACE INTO aliases VALUES (?,?,?,?,?,?,?)",
        (entity_id, normalized, label, status, origin, pid, rev),
    )


def apply_approval(
    db: sqlite3.Connection,
    original: Proposal,
    approved: Proposal,
    entity_ids: dict[str, str],
    explicit_links: set[int],
    alias_edits: set[int],
    event_id: tuple[str, int],
) -> None:
    """Run inside the feedback transaction; repeated calls are no-ops."""
    pid, rev = event_id
    for index, entity in enumerate(approved.entities):
        entity_id = entity_ids.get(entity.ref)
        if entity_id is None:
            continue
        before = original.entities[index]
        created = before.entity_id is None and index not in explicit_links
        row = db.execute(
            "SELECT name FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        if row is None:
            # New entity, or an existing ID recorded before the registry existed.
            db.execute(
                "INSERT INTO entities VALUES (?,?,?,?,?)",
                (entity_id, entity.type, entity.name, pid, rev),
            )
            canonical = entity.name
            _demote_ai(db, entity_id, normalize_name(canonical))
        else:
            canonical = row[0]
        labels: list[tuple[str, str]] = []
        if index in explicit_links:
            # Only the mention the person linked; AI-proposed aliases excluded.
            labels.append((entity.name, "human"))
        if index in alias_edits:
            labels.extend((alias, "human") for alias in entity.aliases)
        elif created:
            labels.extend((alias, "ai") for alias in entity.aliases)
        for label, origin in labels:
            _add_alias(db, entity_id, canonical, label, origin, (pid, rev))


def build_index(db: sqlite3.Connection) -> EntityIndex:
    entities = []
    for entity_id, kind, name in db.execute(
        "SELECT entity_id,type,name FROM entities ORDER BY entity_id"
    ):
        aliases = [
            alias
            for (alias,) in db.execute(
                "SELECT alias FROM aliases WHERE entity_id=? AND status='active' "
                "ORDER BY rowid",
                (entity_id,),
            )
        ]
        entities.append(
            IndexedEntity(entity_id=entity_id, type=kind, name=name, aliases=aliases)
        )
    return EntityIndex(entities=entities)


def conflicts(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in db.execute(
            "SELECT entity_id,alias FROM aliases WHERE status='conflict' ORDER BY rowid"
        )
    ]
