"""Minimal approved Entity/alias registry, the source of EntityIndex (#27).

Rows come only from approved review snapshots. New aliases come from explicit
human corrections (ask choice or alias edit) or from a newly approved entity.
An alias colliding with another entity's name or active alias is stored with
status=conflict and excluded from the index. Entities are never merged, and
reject/revise never change the registry. Relations are out of scope.
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
        proposal_id TEXT NOT NULL, rev INTEGER NOT NULL,
        PRIMARY KEY(entity_id, normalized))""",
    "CREATE INDEX IF NOT EXISTS aliases_normalized ON aliases(normalized, status)",
)


def _taken(db: sqlite3.Connection, entity_id: str, normalized: str) -> bool:
    """True when another entity already answers to this normalized label."""
    for (name,) in db.execute(
        "SELECT name FROM entities WHERE entity_id<>?", (entity_id,)
    ):
        if normalize_name(name) == normalized:
            return True
    row = db.execute(
        "SELECT 1 FROM aliases WHERE normalized=? AND entity_id<>? AND status='active'",
        (normalized, entity_id),
    ).fetchone()
    return row is not None


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
        else:
            canonical = row[0]
        if created:
            labels = list(entity.aliases)
        elif index in explicit_links:
            labels = [entity.name, *entity.aliases]
        elif index in alias_edits:
            labels = list(entity.aliases)
        else:
            labels = []
        for label in labels:
            normalized = normalize_name(label)
            if not normalized or normalized == normalize_name(canonical):
                continue
            if db.execute(
                "SELECT 1 FROM aliases WHERE entity_id=? AND normalized=?",
                (entity_id, normalized),
            ).fetchone():
                continue
            status = "conflict" if _taken(db, entity_id, normalized) else "active"
            db.execute(
                "INSERT INTO aliases VALUES (?,?,?,?,?,?)",
                (entity_id, normalized, label, status, pid, rev),
            )


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
