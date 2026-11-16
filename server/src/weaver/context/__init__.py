"""Canonical tag dictionary and transcript name candidates.

Wiki snapshots and learned corrections retain their v1 tables. Version 2 adds
durable edits and hidden source names; initialization upgrades v1 in place.
Explicit edits take precedence over later wiki snapshots.
"""

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

Kind = Literal["person", "organization", "project", "topic"]
Source = Literal["learn", "wiki", "contact"]
_KINDS = {"person", "organization", "project", "topic"}
_VERSION = 2


@dataclass(frozen=True)
class Page:
    """A Wiki page's name and code-parsed frontmatter for a full snapshot."""

    name: str
    kind: Kind
    aliases: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class ContextEntry:
    """Canonical entry with aliases, speech hints and explicit-edit provenance."""

    name: str
    kind: Kind
    aliases: tuple[str, ...] = ()
    misrecognitions: tuple[str, ...] = ()
    summary: str = ""
    source: Source = "wiki"
    human_edited: bool = False


class SchemaVersionError(RuntimeError):
    """The stored dictionary is not a supported schema version."""


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _key(value: str) -> str:
    return _nfc(value).casefold()


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
        self._readonly = False
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
            if version in (0, 1):
                db.execute(
                    "CREATE TABLE IF NOT EXISTS edited (name TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                db.execute("CREATE TABLE IF NOT EXISTS hidden (name TEXT PRIMARY KEY)")
                db.execute(f"PRAGMA user_version = {_VERSION}")
            elif version != _VERSION:
                raise SchemaVersionError("unsupported context database version")

    @classmethod
    def open_readonly(cls, path: Path) -> "ContextDictionary":
        """Reuse dictionary queries without initialization or write transactions."""
        if not path.is_file():
            raise FileNotFoundError(path)
        dictionary = cls.__new__(cls)
        dictionary._path = path.resolve()
        dictionary._readonly = True
        return dictionary

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = (
            sqlite3.connect(f"{self._path.as_uri()}?mode=ro", uri=True, timeout=0.1)
            if self._readonly
            else sqlite3.connect(self._path, timeout=30)
        )
        try:
            if self._readonly and db.execute("PRAGMA user_version").fetchone()[
                0
            ] not in (1, _VERSION):
                raise SchemaVersionError("unsupported context database version")
            with db:
                yield db
        finally:
            db.close()

    def learn(self, observed: str, canonical: str, kind: Kind) -> None:
        """Record a correction; the latest correction owns a conflicting spelling."""
        canonical = unicodedata.normalize("NFC", canonical)
        observed = unicodedata.normalize("NFC", observed)
        resolved = self.resolve(canonical)
        if resolved:
            canonical = resolved.name
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
        pages = [
            replace(p, name=_nfc(p.name), aliases=tuple(_nfc(a) for a in p.aliases))
            for p in pages
        ]
        names: set[str] = set()
        for page in pages:
            _validate(page.name, page.kind)
            if _key(page.name) in names or any(
                not alias.strip() for alias in page.aliases
            ):
                raise ValueError("duplicate page or empty alias")
            names.add(_key(page.name))
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

    def list_entries(self, limit: int | None = None) -> list[ContextEntry]:
        """List canonical entries and aliases in stable name order (v1 reads supported)."""
        if limit is not None and limit < 0:
            raise ValueError("limit must be nonnegative")
        with self._connect() as db:
            db.execute("BEGIN")
            entries: dict[str, ContextEntry] = {}
            for name, kind in db.execute(
                "SELECT name, kind FROM learned ORDER BY name"
            ):
                entries[_key(name)] = ContextEntry(_nfc(name), kind, source="learn")
            for name, kind, summary in db.execute(
                "SELECT name, kind, summary FROM wiki ORDER BY name"
            ):
                entries[_key(name)] = ContextEntry(_nfc(name), kind, summary=summary)
            aliases: dict[str, list[str]] = {}
            corrections: dict[str, list[str]] = {}
            for name, alias in db.execute(
                "SELECT name, alias FROM wiki_alias ORDER BY alias"
            ):
                aliases.setdefault(_key(name), []).append(_nfc(alias))
            for observed, name in db.execute(
                "SELECT observed, name FROM corrections ORDER BY observed"
            ):
                corrections.setdefault(_key(name), []).append(_nfc(observed))
            entries = {
                key: replace(
                    entry,
                    aliases=tuple(aliases.get(key, ())),
                    misrecognitions=tuple(corrections.get(key, ())),
                )
                for key, entry in entries.items()
            }
            if db.execute("PRAGMA user_version").fetchone()[0] == _VERSION:
                for (name,) in db.execute("SELECT name FROM hidden"):
                    entries.pop(name, None)
                for name, payload in db.execute("SELECT name, payload FROM edited"):
                    data = json.loads(payload)
                    data["aliases"] = tuple(data["aliases"])
                    data["misrecognitions"] = tuple(
                        sorted(
                            set(data["misrecognitions"])
                            | set(corrections.get(name, ()))
                        )
                    )
                    for alias in data["aliases"]:
                        entries.pop(_key(alias), None)
                    entries[name] = ContextEntry(**data)
        result = sorted(entries.values(), key=lambda entry: entry.name)
        return result if limit is None else result[:limit]

    def resolve(self, name: str) -> ContextEntry | None:
        """Resolve canonical names or aliases using NFC and Unicode casefold.

        Exact canonical names win over colliding wiki aliases. Learned speech
        misrecognitions remain transcript candidates, not tag aliases.
        """
        key = _key(name)
        entries = self.list_entries()
        return next((e for e in entries if _key(e.name) == key), None) or next(
            (e for e in entries if any(_key(a) == key for a in e.aliases)), None
        )

    def kind_of(self, name: str) -> Kind | None:
        """Return the resolved canonical entry's kind, including aliases."""
        entry = self.resolve(name)
        return entry.kind if entry else None

    def _require(self, name: str) -> ContextEntry:
        entry = self.resolve(name)
        if entry is None:
            raise ValueError("unknown dictionary entry")
        return entry

    def _save(
        self,
        entry: ContextEntry,
        hidden: tuple[str, ...] = (),
        *,
        create_only: bool = False,
    ) -> ContextEntry:
        _validate(entry.name, entry.kind)
        labels = {_key(entry.name), *(_key(a) for a in entry.aliases)}
        if any(not a.strip() for a in entry.aliases):
            raise ValueError("alias must be nonempty")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if create_only:
                existing = self.resolve(entry.name)
                if existing:
                    return existing
            # Serialize editing and validate against a consistent snapshot.
            for other in self.list_entries():
                if _key(other.name) in {_key(entry.name), *map(_key, hidden)}:
                    continue
                if labels & {_key(other.name), *map(_key, other.aliases)}:
                    raise ValueError("dictionary name or alias collision")
            db.executemany(
                "INSERT OR IGNORE INTO hidden VALUES (?)", [(_key(n),) for n in hidden]
            )
            db.executemany(
                "DELETE FROM edited WHERE name=?", [(_key(n),) for n in hidden]
            )
            for observed, name in db.execute(
                "SELECT observed, name FROM corrections"
            ).fetchall():
                if _key(name) in {_key(entry.name), *map(_key, hidden)}:
                    db.execute(
                        "UPDATE corrections SET name=? WHERE observed=?",
                        (entry.name, observed),
                    )
            db.execute(
                "INSERT INTO edited VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                (
                    _key(entry.name),
                    json.dumps(
                        asdict(replace(entry, misrecognitions=())), ensure_ascii=False
                    ),
                ),
            )
        return entry

    def add_entry(
        self,
        name: str,
        kind: Kind,
        aliases: tuple[str, ...] = (),
        *,
        human_edited: bool = True,
    ) -> ContextEntry:
        """Add an entry; repeating an existing canonical name is a safe no-op.

        Automatic callers use human_edited=False. Existing entries are never
        overwritten by add; use explicit editing operations to change them.
        """
        _validate(name, kind)
        existing = self.resolve(name)
        if existing:
            return existing
        return self._save(
            ContextEntry(
                _nfc(name),
                kind,
                tuple(sorted({_nfc(a) for a in aliases})),
                source="learn",
                human_edited=human_edited,
            ),
            create_only=True,
        )

    def rename(self, name: str, new_name: str) -> ContextEntry:
        """Rename while retaining the old canonical name as an alias."""
        entry = self._require(name)
        target = self.resolve(new_name)
        if target and target.name != entry.name:
            raise ValueError("dictionary name or alias collision")
        return self._save(
            replace(
                entry,
                name=_nfc(new_name),
                aliases=tuple(
                    sorted((set(entry.aliases) | {entry.name}) - {_nfc(new_name)})
                ),
                human_edited=True,
            ),
            (entry.name,),
        )

    def set_kind(self, name: str, kind: Kind) -> ContextEntry:
        return self._save(replace(self._require(name), kind=kind, human_edited=True))

    def add_alias(self, name: str, alias: str) -> ContextEntry:
        entry = self._require(name)
        return self._save(
            replace(
                entry,
                aliases=tuple(sorted(set(entry.aliases) | {_nfc(alias)})),
                human_edited=True,
            )
        )

    def remove_alias(self, name: str, alias: str) -> ContextEntry:
        entry = self._require(name)
        return self._save(
            replace(
                entry,
                aliases=tuple(a for a in entry.aliases if _key(a) != _key(alias)),
                human_edited=True,
            )
        )

    def merge(self, source: str, target: str) -> ContextEntry:
        """Merge source into target, preserving both names, aliases and corrections."""
        a, b = self._require(source), self._require(target)
        if a.name == b.name:
            return b
        return self._save(
            replace(
                b,
                aliases=tuple(sorted(set((*a.aliases, *b.aliases, a.name)) - {b.name})),
                misrecognitions=tuple(
                    sorted(set((*a.misrecognitions, *b.misrecognitions)))
                ),
                human_edited=True,
            ),
            (a.name,),
        )

    def candidates(
        self, text: str, contact_name: str | None = None, limit: int = 10
    ) -> list[ContextEntry]:
        """Select similar token matches, ordered by distance, with contact first."""
        if limit < 0:
            raise ValueError("limit must be nonnegative")
        if not limit:
            return []
        tokens = re.findall(r"[^\W_]+", _nfc(text), re.UNICODE)
        entries = self.list_entries()
        ranked: list[tuple[int, int, str, ContextEntry]] = []
        for entry in entries:
            name = entry.name
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
            if contact is None:
                contact = self.resolve(contact_name)
            contact = contact or ContextEntry(
                _nfc(contact_name), "person", source="contact"
            )
            result = [
                contact,
                *(entry for entry in result if entry.name != contact.name),
            ]
        return result[:limit]
