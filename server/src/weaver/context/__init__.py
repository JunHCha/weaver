"""Local context dictionary for selecting names relevant to a transcript.

Wiki sync replaces the complete wiki snapshot atomically; learned corrections
survive wiki updates and deletions. No legacy entity data is imported.
"""

import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal["person", "organization", "project", "topic"]
Source = Literal["learn", "wiki", "contact"]
_KINDS = {"person", "organization", "project", "topic"}
_VERSION = 1


@dataclass(frozen=True)
class Page:
    """A Wiki page's name and code-parsed frontmatter for a full snapshot."""

    name: str
    kind: Kind
    aliases: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class Entry:
    """A prompt candidate, with its active Wiki and learned contributions."""

    name: str
    kind: Kind
    aliases: tuple[str, ...] = ()
    misrecognitions: tuple[str, ...] = ()
    summary: str = ""
    source: Source = "wiki"


class SchemaVersionError(RuntimeError):
    """The stored dictionary is not a supported schema version."""


def _validate(name: str, kind: Kind) -> None:
    if not name.strip() or kind not in _KINDS:
        raise ValueError("name must be nonempty and kind must be supported")


def _distance(a: str, b: str) -> int:
    a = unicodedata.normalize("NFD", a.casefold())
    b = unicodedata.normalize("NFD", b.casefold())
    row = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        next_row = [i]
        for j, other in enumerate(b, 1):
            next_row.append(
                min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (char != other))
            )
        row = next_row
    return row[-1]


def _score(label: str, tokens: list[str]) -> int | None:
    label = label.casefold()
    threshold = min(2, max(1, len(unicodedata.normalize("NFD", label)) // 4))
    best: int | None = None
    for token in tokens:
        token = token.casefold()
        for size in range(max(1, len(label) - 1), min(len(token), len(label) + 1) + 1):
            for start in range(len(token) - size + 1):
                distance = _distance(label, token[start : start + size])
                if distance <= threshold and (best is None or distance < best):
                    best = distance
    return best


class ContextDictionary:
    """SQLite dictionary stored under state_root; each operation is atomic."""

    def __init__(self, state_root: Path) -> None:
        state_root.mkdir(parents=True, exist_ok=True)
        self._path = state_root / "context.sqlite3"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    raise SchemaVersionError("unversioned context database")
                db.execute(
                    "CREATE TABLE wiki (name TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE wiki_alias (name TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY(name, alias))"
                )
                db.execute(
                    "CREATE TABLE learned (name TEXT PRIMARY KEY, kind TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE corrections (observed TEXT PRIMARY KEY, name TEXT NOT NULL)"
                )
                db.execute(f"PRAGMA user_version = {_VERSION}")
            elif version != _VERSION:
                raise SchemaVersionError("unsupported context database version")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def learn(self, observed: str, canonical: str, kind: Kind) -> None:
        """Record a correction; the latest correction owns a conflicting spelling."""
        _validate(canonical, kind)
        if not observed.strip():
            raise ValueError("observed must be nonempty")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO learned VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind",
                (canonical, kind),
            )
            if observed != canonical:
                db.execute(
                    "INSERT INTO corrections VALUES (?, ?) ON CONFLICT(observed) DO UPDATE SET name=excluded.name",
                    (observed, canonical),
                )

    def sync_from_pages(self, pages: list[Page]) -> None:
        """Replace the complete Wiki snapshot; missing pages are removed."""
        names: set[str] = set()
        for page in pages:
            _validate(page.name, page.kind)
            if page.name in names or any(not alias.strip() for alias in page.aliases):
                raise ValueError("duplicate page or empty alias")
            names.add(page.name)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM wiki_alias")
            db.execute("DELETE FROM wiki")
            for page in pages:
                db.execute(
                    "INSERT INTO wiki VALUES (?, ?, ?)",
                    (page.name, page.kind, page.summary),
                )
                db.executemany(
                    "INSERT INTO wiki_alias VALUES (?, ?)",
                    [(page.name, alias) for alias in set(page.aliases)],
                )

    def kind_of(self, name: str) -> Kind | None:
        """Return the exact canonical name's kind, preferring its wiki entry."""
        with self._connect() as db:
            row = db.execute(
                "SELECT kind FROM (SELECT kind, 0 AS priority FROM wiki WHERE name=? "
                "UNION ALL SELECT kind, 1 AS priority FROM learned WHERE name=?) "
                "ORDER BY priority LIMIT 1",
                (name, name),
            ).fetchone()
        if row is None:
            return None
        kind: Kind = row[0]
        return kind

    def candidates(
        self, text: str, contact_name: str | None = None, limit: int = 10
    ) -> list[Entry]:
        """Select similar token matches, ordered by distance, with contact first."""
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        if not limit:
            return []
        tokens = re.findall(r"[^\W_]+", text, re.UNICODE)
        with self._connect() as db:
            db.execute("BEGIN")
            wiki = {
                name: (kind, summary)
                for name, kind, summary in db.execute(
                    "SELECT name, kind, summary FROM wiki"
                )
            }
            learned = dict(db.execute("SELECT name, kind FROM learned"))
            aliases: dict[str, list[str]] = {}
            corrections: dict[str, list[str]] = {}
            for name, alias in db.execute(
                "SELECT name, alias FROM wiki_alias ORDER BY alias"
            ):
                aliases.setdefault(name, []).append(alias)
            for observed, name in db.execute(
                "SELECT observed, name FROM corrections ORDER BY observed"
            ):
                corrections.setdefault(name, []).append(observed)
        ranked: list[tuple[int, int, str, Entry]] = []
        names = wiki.keys() | learned.keys()
        for name in names:
            kind, summary = wiki.get(name, (learned.get(name, "person"), ""))
            entry = Entry(
                name,
                kind,
                tuple(aliases.get(name, ())),
                tuple(corrections.get(name, ())),
                summary,
                "wiki" if name in wiki else "learn",
            )
            scores = [
                _score(label, tokens)
                for label in (name, *entry.aliases, *entry.misrecognitions)
            ]
            matches = [score for score in scores if score is not None]
            if matches:
                ranked.append(
                    (
                        min(matches),
                        0
                        if any(
                            _score(label, tokens) == 0
                            for label in entry.misrecognitions
                        )
                        else 1,
                        name,
                        entry,
                    )
                )
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        result = [entry for _, _, _, entry in ranked]
        if contact_name and contact_name.strip():
            contact = next(
                (entry for _, _, name, entry in ranked if name == contact_name), None
            )
            if contact is None and contact_name in names:
                kind, summary = wiki.get(
                    contact_name, (learned.get(contact_name, "person"), "")
                )
                contact = Entry(
                    contact_name,
                    kind,
                    tuple(aliases.get(contact_name, ())),
                    tuple(corrections.get(contact_name, ())),
                    summary,
                    "wiki" if contact_name in wiki else "learn",
                )
            result = [
                contact or Entry(contact_name, "person", source="contact"),
                *(entry for entry in result if entry.name != contact_name),
            ]
        return result[:limit]
