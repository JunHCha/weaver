"""Canonical tag dictionary and transcript name candidates.

Wiki snapshots and learned corrections retain their v1 tables. Version 2 adds
durable edits and hidden source names; initialization upgrades v1 in place.
Explicit edits take precedence over later wiki snapshots.
"""

import json
import logging
import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from weaver.context.tree import TreeNode

Kind = Literal["person", "organization", "project", "area", "topic"]
Source = Literal["learn", "wiki", "contact", "tree"]
_KINDS = {"person", "organization", "project", "area", "topic"}
_VERSION = 2


@dataclass(frozen=True)
class Page:
    """A Wiki page's name and code-parsed frontmatter for a full snapshot."""

    name: str
    kind: Kind
    aliases: tuple[str, ...] = ()
    summary: str = ""
    source_ids: tuple[str, ...] = ()


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
    promoted: bool = False
    archived: bool = False
    promotion_reverted: bool = False


class SchemaVersionError(RuntimeError):
    """The stored dictionary is not a supported schema version."""


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _key(value: str) -> str:
    return "".join(_nfc(value).casefold().split())


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


class DictionarySnapshot:
    """One immutable dictionary read; resolve arbitrarily many labels without I/O."""

    def __init__(self, entries: list[ContextEntry]) -> None:
        self.entries = tuple(entries)
        self._names: dict[str, ContextEntry] = {}
        for entry in sorted(entries, key=lambda e: e.name):
            self._names.setdefault(_key(entry.name), entry)
        self._aliases: dict[str, ContextEntry] = {}
        for entry in sorted(entries, key=lambda e: e.name):
            for alias in entry.aliases:
                self._aliases.setdefault(_key(alias), entry)

    def list_entries(self) -> list[ContextEntry]:
        return list(self.entries)

    def resolve(self, name: str) -> ContextEntry | None:
        key = _key(name)
        return self._names.get(key) or self._aliases.get(key)

    @property
    def has_tree(self) -> bool:
        return any(e.source == "tree" for e in self.entries)

    def area_of(self, tag: str) -> ContextEntry | None:
        from weaver.context import tree

        entry = self.resolve(tag)
        if entry is None:
            return None
        if entry.source == "tree":
            path = tree.area_of(entry.name)
            return None if path is None else self.resolve(path)
        return entry if entry.kind == "area" else None

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


@dataclass(frozen=True)
class _Edit:
    name: str
    bases: tuple[str, ...]
    fallback_kind: Kind
    kind: Kind | None = None
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    human_edited: bool = True
    origin_wiki: bool = False
    automatic_aliases: tuple[str, ...] = ()


def _decode_edit(payload: str) -> _Edit:
    data = json.loads(payload)
    if "bases" not in data:
        # Read the initial v2 payload without retaining its frozen wiki summary.
        return _Edit(
            data["name"],
            (data["name"], *data["aliases"]),
            data["kind"],
            data["kind"] if data["human_edited"] else None,
            tuple(data["aliases"]),
            human_edited=data["human_edited"],
            origin_wiki=data["source"] == "wiki",
        )
    for field in ("bases", "added", "removed", "automatic_aliases"):
        data[field] = tuple(data.get(field, ()))
    return _Edit(**data)


logger = logging.getLogger(__name__)


class EntryAlreadyExists(ValueError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"dictionary name or alias collision: {name}")


class ContextDictionary:
    """SQLite dictionary; read/modify/write operations share a write transaction."""

    def __init__(self, state_root: Path, *, archive_root: Path | None = None) -> None:
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
            if version in (0, 1):
                self._upgrade(db)
            elif version != _VERSION:
                raise SchemaVersionError("unsupported context database version")
            db.execute(
                "CREATE TABLE IF NOT EXISTS tree_nodes "
                "(name TEXT PRIMARY KEY, kind TEXT NOT NULL, sphere TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS origin_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            if archive_root is not None:
                db.execute(
                    "INSERT OR REPLACE INTO origin_config VALUES ('archive', ?)",
                    (str(archive_root.resolve()),),
                )
            binding = db.execute(
                "SELECT value FROM origin_config WHERE key='archive'"
            ).fetchone()
            self._archive = Path(binding[0]) if binding else None
            db.execute(
                "CREATE TABLE IF NOT EXISTS project_lifecycle "
                "(name TEXT PRIMARY KEY, promoted INTEGER NOT NULL, archived INTEGER NOT NULL)"
            )
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(project_lifecycle)")
            }
            if "reverted" not in columns:
                db.execute(
                    "ALTER TABLE project_lifecycle ADD COLUMN reverted INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS entry_origins (name TEXT PRIMARY KEY, source_ids TEXT NOT NULL, unknown_at TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(entry_origins)")}
            if "unknown_webex" not in columns:
                db.execute("ALTER TABLE entry_origins ADD COLUMN unknown_webex INTEGER")
                db.execute(
                    "UPDATE entry_origins SET unknown_webex=? WHERE unknown_at IS NOT NULL",
                    (int(self._has_webex()),),
                )
            for entry in self._load(db)[0].entries:
                db.execute(
                    "INSERT OR IGNORE INTO entry_origins (name, source_ids, unknown_webex) VALUES (?, '[]', ?)",
                    (_key(entry.name), int(self._has_webex())),
                )
        with self._path.open("rb") as file:
            os.fchmod(file.fileno(), 0o600)

    @staticmethod
    def _upgrade(db: sqlite3.Connection) -> None:
        # DDL and version change remain in the caller's single transaction.
        db.execute(
            "CREATE TABLE IF NOT EXISTS edited (name TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        db.execute("CREATE TABLE IF NOT EXISTS hidden (name TEXT PRIMARY KEY)")
        db.execute(f"PRAGMA user_version = {_VERSION}")

    @classmethod
    def open_readonly(cls, path: Path) -> "ContextDictionary":
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
            with db:
                yield db
        finally:
            db.close()

    def _load(
        self, db: sqlite3.Connection
    ) -> tuple[DictionarySnapshot, dict[str, _Edit]]:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, _VERSION):
            raise SchemaVersionError("unsupported context database version")
        entries: dict[str, ContextEntry] = {}
        # Tree nodes load first, so any later source with the same key wins.
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tree_nodes'"
        ).fetchone():
            for n, k in db.execute("SELECT name, kind FROM tree_nodes ORDER BY name"):
                entries[_key(n)] = ContextEntry(_nfc(n), k, source="tree")
        entries.update(
            {
                _key(n): ContextEntry(_nfc(n), k, source="learn")
                for n, k in db.execute(
                    "SELECT name, kind FROM learned ORDER BY name DESC"
                )
            }
        )
        wiki = {
            _key(n): ContextEntry(_nfc(n), k, summary=summary)
            for n, k, summary in db.execute(
                "SELECT name, kind, summary FROM wiki ORDER BY name DESC"
            )
        }
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
        wiki = {
            k: replace(e, aliases=tuple(aliases.get(k, ()))) for k, e in wiki.items()
        }
        entries.update(wiki)
        edits = (
            {
                _key(k): _decode_edit(p)
                for k, p in db.execute("SELECT name, payload FROM edited ORDER BY name")
            }
            if version == _VERSION
            else {}
        )
        # Only a renamed/merged source is hidden, never an unrelated page whose
        # canonical name happens to equal an edited alias. Canonical names win.
        for key, edit in edits.items():
            if not edit.human_edited and key in wiki:
                continue
            bases = [wiki[_key(n)] for n in edit.bases if _key(n) in wiki]
            base = bases[0] if bases else entries.get(key)
            current_aliases = {a for e in bases for a in e.aliases}
            current_aliases = {
                a
                for a in current_aliases
                if _key(a) not in {_key(r) for r in edit.removed}
            }
            current_aliases.update(edit.added)
            current_aliases = {a for a in current_aliases if _key(a) != key}
            if edit.human_edited:
                for name in edit.bases:
                    if _key(name) != key and _key(name) in {
                        _key(a) for a in current_aliases
                    }:
                        entries.pop(_key(name), None)
            entries[key] = ContextEntry(
                edit.name,
                edit.kind or (base.kind if base else edit.fallback_kind),
                tuple(sorted(current_aliases)),
                summary=base.summary if base else "",
                source="wiki" if bases else "learn",
                human_edited=edit.human_edited,
            )
        # Automatic aliases never freeze wiki fields or mark a human edit.
        for key, edit in edits.items():
            if key in entries:
                entry = entries[key]
                removed = {_key(a) for a in edit.removed}
                entries[key] = replace(
                    entry,
                    aliases=tuple(
                        sorted(
                            set(entry.aliases)
                            | {
                                a
                                for a in edit.automatic_aliases
                                if _key(a) not in removed and _key(a) != key
                            }
                        )
                    ),
                )
        entries = {
            k: replace(e, misrecognitions=tuple(corrections.get(k, ())))
            for k, e in entries.items()
        }
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_lifecycle'"
        ).fetchone():
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(project_lifecycle)")
            }
            lifecycle = {
                row[0]: tuple(bool(value) for value in row[1:])
                for row in db.execute(
                    "SELECT name, promoted, archived, "
                    + ("reverted" if "reverted" in columns else "0")
                    + " FROM project_lifecycle"
                )
            }
            for key, entry in entries.items():
                values = [
                    lifecycle[label]
                    for label in (key, *map(_key, entry.aliases))
                    if label in lifecycle
                ]
                if key in lifecycle:
                    values = [lifecycle[key]]
                if values:
                    entries[key] = replace(
                        entry,
                        promoted=any(v[0] for v in values),
                        archived=any(v[1] for v in values),
                        promotion_reverted=any(v[2] for v in values),
                    )
        return DictionarySnapshot(sorted(entries.values(), key=lambda e: e.name)), edits

    def snapshot(self) -> DictionarySnapshot:
        """Read once in a transaction, then resolve names/candidates in memory."""
        with self._connect() as db:
            db.execute("BEGIN")
            return self._load(db)[0]

    def list_entries(self, limit: int | None = None) -> list[ContextEntry]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be nonnegative")
        entries = self.snapshot().list_entries()
        return entries if limit is None else entries[:limit]

    def resolve(self, name: str) -> ContextEntry | None:
        return self.snapshot().resolve(name)

    def kind_of(self, name: str) -> Kind | None:
        entry = self.resolve(name)
        return entry.kind if entry else None

    def candidates(
        self, text: str, contact_name: str | None = None, limit: int = 10
    ) -> list[ContextEntry]:
        return self.snapshot().candidates(text, contact_name, limit)

    @contextmanager
    def _writing(
        self,
        source_ids: tuple[str, ...] = (),
        page_origins: dict[str, tuple[str, ...]] | None = None,
        *,
        human: bool = False,
    ) -> Iterator[tuple[sqlite3.Connection, DictionarySnapshot, dict[str, _Edit]]]:
        from weaver.llm.policy import HUMAN_DICTIONARY_SOURCE

        claimed = set(source_ids).union(*(page_origins or {}).values())
        if HUMAN_DICTIONARY_SOURCE in claimed and not human:
            raise ValueError("human dictionary source requires a human edit")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot, edits = self._load(db)
            yield db, snapshot, edits
            before = {_key(e.name): e for e in snapshot.entries}
            for entry in self._load(db)[0].entries:
                if entry.source == "tree":
                    continue  # Vault paths carry no transcript lineage.
                prior = before.get(_key(entry.name))

                def content(value: ContextEntry) -> ContextEntry:
                    return replace(
                        value, promoted=False, archived=False, promotion_reverted=False
                    )

                if prior is None or content(prior) != content(entry):
                    self._record_origin(
                        db,
                        entry.name,
                        (page_origins or {}).get(_key(entry.name), source_ids),
                    )

    def _has_webex(self) -> bool:
        if self._archive is None:
            return True  # No archive binding is not evidence of a non-Webex write.
        index = self._archive / "index.sqlite3"
        if not index.exists():
            return False
        try:
            db = sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True)
            try:
                return (
                    db.execute(
                        "SELECT 1 FROM sources WHERE json_extract(source_json, '$.channel')='webex' LIMIT 1"
                    ).fetchone()
                    is not None
                )
            finally:
                db.close()
        except sqlite3.Error:
            return True

    def _record_origin(
        self, db: sqlite3.Connection, name: str, source_ids: tuple[str, ...]
    ) -> None:
        from weaver.llm.policy import UNKNOWN_LINEAGE

        row = db.execute(
            "SELECT source_ids, unknown_webex FROM entry_origins WHERE name=?",
            (_key(name),),
        ).fetchone()
        origins = set(json.loads(row[0])) if row else set()
        origins.update(set(source_ids) - UNKNOWN_LINEAGE)
        unknown = row[1] if row else None
        if not source_ids or UNKNOWN_LINEAGE.intersection(source_ids):
            unknown = int(bool(unknown) or self._has_webex())
        db.execute(
            "INSERT OR REPLACE INTO entry_origins (name, source_ids, unknown_webex) VALUES (?, ?, ?)",
            (_key(name), json.dumps(sorted(origins)), unknown),
        )

    def policy_origins(self) -> tuple[set[str], bool]:
        """A persisted write-time flag proves unknown content non-Webex."""
        ids: set[str] = set()
        unknown = False
        with self._connect() as db:
            db.execute("BEGIN")
            entries = self._load(db)[0].entries
            exists = any(
                row[1] == "unknown_webex"
                for row in db.execute("PRAGMA table_info(entry_origins)")
            )
            for entry in entries:
                if entry.source == "tree":
                    continue
                row = (
                    db.execute(
                        "SELECT source_ids, unknown_webex FROM entry_origins WHERE name=?",
                        (_key(entry.name),),
                    ).fetchone()
                    if exists
                    else None
                )
                if row:
                    ids.update(json.loads(row[0]))
                if row is None or row[1] == 1:
                    unknown = True
        return ids, unknown

    @staticmethod
    def _require(snapshot: DictionarySnapshot, name: str) -> ContextEntry:
        entry = snapshot.resolve(name)
        if entry is None:
            raise ValueError("unknown dictionary entry")
        return entry

    @classmethod
    def _require_editable(cls, snapshot: DictionarySnapshot, name: str) -> ContextEntry:
        entry = cls._require(snapshot, name)
        if entry.source == "tree":
            raise ValueError("tree node is read-only")
        return entry

    @staticmethod
    def _edit_for(entry: ContextEntry, edits: dict[str, _Edit]) -> _Edit:
        return edits.get(_key(entry.name)) or _Edit(
            entry.name, (entry.name,), entry.kind, origin_wiki=entry.source == "wiki"
        )

    def _save(
        self,
        db: sqlite3.Connection,
        snapshot: DictionarySnapshot,
        edit: _Edit,
        hidden: tuple[str, ...] = (),
    ) -> ContextEntry:
        _validate(edit.name, edit.kind or edit.fallback_kind)
        if any(not a.strip() for a in edit.added):
            raise ValueError("alias must be nonempty")
        own_keys = {_key(edit.name), *map(_key, hidden)}
        prior_aliases = {
            _key(alias)
            for prior in snapshot.entries
            if _key(prior.name) in own_keys
            for alias in prior.aliases
        }
        new_aliases = (
            set(map(_key, (*edit.added, *edit.automatic_aliases))) - prior_aliases
        )
        prior_name = any(_key(e.name) == _key(edit.name) for e in snapshot.entries)
        for other in snapshot.entries:
            if _key(other.name) in own_keys:
                continue
            other_labels = {_key(other.name), *map(_key, other.aliases)}
            if (
                _key(edit.name) in other_labels and not prior_name
            ) or new_aliases & other_labels:
                raise EntryAlreadyExists(other.name)
        hidden_keys = {_key(edit.name), *map(_key, hidden)}
        for (stored_key,) in db.execute("SELECT name FROM edited").fetchall():
            if _key(stored_key) in hidden_keys:
                db.execute("DELETE FROM edited WHERE name=?", (stored_key,))
        learned = [
            (name, kind)
            for name, kind in db.execute("SELECT name, kind FROM learned")
            if _key(name) in own_keys
        ]
        if learned:
            fallback = next(
                (kind for name, kind in learned if _key(name) == _key(edit.name)),
                learned[0][1],
            )
            db.executemany(
                "DELETE FROM learned WHERE name=?", [(name,) for name, _ in learned]
            )
            db.execute("INSERT INTO learned VALUES (?, ?)", (edit.name, fallback))
        for observed, name in db.execute(
            "SELECT observed, name FROM corrections"
        ).fetchall():
            if _key(name) in own_keys:
                db.execute(
                    "UPDATE corrections SET name=? WHERE observed=?",
                    (edit.name, observed),
                )
        db.execute(
            "INSERT INTO edited VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
            (_key(edit.name), json.dumps(asdict(edit), ensure_ascii=False)),
        )
        return self._require(self._load(db)[0], edit.name)

    def add_entry(
        self,
        name: str,
        kind: Kind,
        aliases: tuple[str, ...] = (),
        *,
        human_edited: bool = True,
        drop_conflicting_aliases: bool = False,
        require_new: bool = False,
        source_ids: tuple[str, ...] = (),
    ) -> ContextEntry:
        """Idempotent creation; automatic approval can discard racing alias conflicts."""
        from weaver.llm.policy import HUMAN_DICTIONARY_SOURCE

        _validate(name, kind)
        with self._writing(source_ids, human=human_edited) as (db, snapshot, _):
            existing = snapshot.resolve(name)
            if existing:
                if require_new:
                    raise EntryAlreadyExists(existing.name)
                if drop_conflicting_aliases and _key(existing.name) != _key(name):
                    logger.warning("skipped conflicting dictionary proposal")
                # Attaching sources to an existing entry is not a human edit.
                carried = tuple(s for s in source_ids if s != HUMAN_DICTIONARY_SOURCE)
                if carried:
                    self._record_origin(db, existing.name, carried)
                return existing
            normalized = tuple(sorted({_nfc(a) for a in aliases}))
            if drop_conflicting_aliases:
                normalized = tuple(a for a in normalized if snapshot.resolve(a) is None)
                if len(normalized) < len(set(aliases)):
                    logger.warning("dropped conflicting dictionary aliases")
            edit = _Edit(
                _nfc(name),
                (_nfc(name),),
                kind,
                kind if human_edited else None,
                normalized,
                human_edited=human_edited,
            )
            return self._save(db, snapshot, edit)

    def set_project_lifecycle(
        self,
        name: str,
        *,
        promoted: bool | None = None,
        archived: bool,
        reverted: bool | None = None,
    ) -> ContextEntry:
        """Persist automatic lifecycle separately from human dictionary overrides."""
        with self._writing() as (db, snapshot, _):
            entry = self._require(snapshot, name)
            if entry.kind != "project":
                raise ValueError("lifecycle requires a project")
            db.execute(
                "INSERT INTO project_lifecycle VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET promoted=excluded.promoted, archived=excluded.archived, reverted=excluded.reverted",
                (
                    _key(entry.name),
                    entry.promoted if promoted is None else promoted,
                    archived,
                    entry.promotion_reverted if reverted is None else reverted,
                ),
            )
            return self._require(self._load(db)[0], entry.name)

    def rename(
        self,
        name: str,
        new_name: str,
        *,
        human_edited: bool = True,
        source_ids: tuple[str, ...] = (),
    ) -> ContextEntry:
        with self._writing(source_ids, human=human_edited) as (db, snapshot, edits):
            entry = self._require_editable(snapshot, name)
            origin = db.execute(
                "SELECT source_ids, unknown_webex FROM entry_origins WHERE name=?",
                (_key(entry.name),),
            ).fetchone()
            if origin and _key(name) != _key(new_name):
                previous = db.execute(
                    "SELECT source_ids, unknown_webex FROM entry_origins WHERE name=?",
                    (_key(new_name),),
                ).fetchone()
                origins = set(json.loads(origin[0])) | (
                    set(json.loads(previous[0])) if previous else set()
                )
                flags = [
                    v
                    for v in (origin[1], previous[1] if previous else None)
                    if v is not None
                ]
                db.execute(
                    "INSERT OR REPLACE INTO entry_origins (name, source_ids, unknown_webex) VALUES (?, ?, ?)",
                    (
                        _key(new_name),
                        json.dumps(sorted(origins)),
                        max(flags) if flags else None,
                    ),
                )
            target = snapshot.resolve(new_name)
            if target and target.name != entry.name:
                raise EntryAlreadyExists(target.name)
            if not human_edited and (entry.source != "learn" or entry.human_edited):
                raise ValueError("automatic rename requires an unedited learned entry")
            edit = self._edit_for(entry, edits)
            edit = replace(
                edit,
                name=_nfc(new_name),
                human_edited=human_edited,
                automatic_aliases=tuple(
                    a
                    for a in edit.automatic_aliases
                    if human_edited or _key(a) != _key(entry.name)
                ),
                added=tuple(
                    sorted(
                        {
                            alias
                            for alias in (
                                set(edit.added)
                                | ({entry.name} if human_edited else set())
                            )
                            if _key(alias) != _key(new_name)
                            and (human_edited or _key(alias) != _key(entry.name))
                        }
                    )
                ),
            )
            result = self._save(db, snapshot, edit, (entry.name,))
            if not human_edited and entry.name != result.name:
                db.execute(
                    "INSERT INTO corrections VALUES (?, ?) ON CONFLICT(observed) DO UPDATE SET name=excluded.name",
                    (entry.name, result.name),
                )
                return self._require(self._load(db)[0], result.name)
            return result

    def set_kind(
        self, name: str, kind: Kind, *, source_ids: tuple[str, ...] = ()
    ) -> ContextEntry:
        with self._writing(source_ids, human=True) as (db, snapshot, edits):
            entry = self._require_editable(snapshot, name)
            return self._save(
                db,
                snapshot,
                replace(self._edit_for(entry, edits), kind=kind, human_edited=True),
            )

    def add_alias(
        self,
        name: str,
        alias: str,
        *,
        human_edited: bool = True,
        source_ids: tuple[str, ...] = (),
    ) -> ContextEntry:
        with self._writing(source_ids, human=human_edited) as (db, snapshot, edits):
            entry = self._require_editable(snapshot, name)
            edit = self._edit_for(entry, edits)
            if not human_edited:
                if not alias.strip():
                    raise ValueError("alias must be nonempty")
                owner = snapshot.resolve(alias)
                if owner:
                    if owner.name != entry.name:
                        raise EntryAlreadyExists(owner.name)
                    return entry
                if _key(alias) in {_key(a) for a in edit.removed}:
                    return entry
                # _save's collision check covers ordinary aliases; check the
                # automatic proposal above before retaining it separately.
                return self._save(
                    db,
                    snapshot,
                    replace(
                        edit,
                        human_edited=entry.human_edited,
                        automatic_aliases=tuple(
                            sorted(set(edit.automatic_aliases) | {_nfc(alias)})
                        ),
                    ),
                )
            edit = replace(
                edit,
                added=tuple(sorted(set(edit.added) | {_nfc(alias)})),
                removed=tuple(a for a in edit.removed if _key(a) != _key(alias)),
                human_edited=True,
            )
            return self._save(db, snapshot, edit)

    def remove_alias(
        self, name: str, alias: str, *, source_ids: tuple[str, ...] = ()
    ) -> ContextEntry:
        with self._writing(source_ids, human=True) as (db, snapshot, edits):
            entry = self._require_editable(snapshot, name)
            edit = self._edit_for(entry, edits)
            edit = replace(
                edit,
                added=tuple(a for a in edit.added if _key(a) != _key(alias)),
                removed=tuple(sorted(set(edit.removed) | {_nfc(alias)})),
                human_edited=True,
            )
            return self._save(db, snapshot, edit)

    def merge(
        self,
        source: str,
        target: str,
        *,
        kind: Kind | None = None,
        source_ids: tuple[str, ...] = (),
    ) -> ContextEntry:
        with self._writing(source_ids, human=True) as (db, snapshot, edits):
            a, b = (
                self._require_editable(snapshot, source),
                self._require_editable(snapshot, target),
            )
            if a.name == b.name:
                return b
            source_edit, edit = self._edit_for(a, edits), self._edit_for(b, edits)
            # Lineage follows the content: the target inherits the source's
            # origins and unknown flag, as rename does.
            rows = [
                db.execute(
                    "SELECT source_ids, unknown_webex FROM entry_origins WHERE name=?",
                    (_key(entry.name),),
                ).fetchone()
                for entry in (a, b)
            ]
            if any(rows):
                merged = set().union(*(json.loads(r[0]) for r in rows if r))
                flags = [r[1] for r in rows if r and r[1] is not None]
                db.execute(
                    "INSERT OR REPLACE INTO entry_origins (name, source_ids, unknown_webex) VALUES (?, ?, ?)",
                    (
                        _key(b.name),
                        json.dumps(sorted(merged)),
                        max(flags) if flags else None,
                    ),
                )
            edit = replace(
                edit,
                bases=tuple(dict.fromkeys((*edit.bases, *source_edit.bases))),
                automatic_aliases=tuple(
                    sorted(
                        set(edit.automatic_aliases) | set(source_edit.automatic_aliases)
                    )
                ),
                kind=kind or b.kind,
                added=tuple(
                    sorted(set((*edit.added, *source_edit.added, a.name)) - {b.name})
                ),
                removed=tuple(sorted(set((*edit.removed, *source_edit.removed)))),
                human_edited=True,
            )
            return self._save(db, snapshot, edit, (a.name,))

    def dismissed_pairs(self) -> list[tuple[str, str]]:
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='dismissed_tag_pairs'"
            ).fetchone()
            return (
                list(db.execute("SELECT a, b FROM dismissed_tag_pairs"))
                if exists
                else []
            )

    def dismiss_pair(self, a: str, b: str) -> None:
        pair = sorted((_key(a), _key(b)))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS dismissed_tag_pairs (a TEXT NOT NULL, b TEXT NOT NULL, PRIMARY KEY(a,b))"
            )
            db.execute("INSERT OR IGNORE INTO dismissed_tag_pairs VALUES (?,?)", pair)

    def learn(
        self,
        observed: str,
        canonical: str,
        kind: Kind,
        *,
        source_ids: tuple[str, ...] = (),
    ) -> None:
        observed, canonical = _nfc(observed), _nfc(canonical)
        _validate(canonical, kind)
        if not observed.strip():
            raise ValueError("observed must be nonempty")
        with self._writing(source_ids) as (db, snapshot, _):
            resolved = snapshot.resolve(canonical)
            canonical = resolved.name if resolved else canonical
            db.execute(
                "INSERT INTO learned VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind",
                (canonical, kind),
            )
            if observed != canonical:
                db.execute(
                    "INSERT INTO corrections VALUES (?, ?) ON CONFLICT(observed) DO UPDATE SET name=excluded.name",
                    (observed, canonical),
                )

    def sync_tree(self, sphere: str, nodes: "list[TreeNode]") -> None:
        """Replace one sphere's read-only tree nodes in a single transaction."""
        with self._writing() as (db, _, _edits):
            db.execute("DELETE FROM tree_nodes WHERE sphere=?", (sphere,))
            db.executemany(
                "INSERT OR REPLACE INTO tree_nodes VALUES (?, ?, ?)",
                [(_nfc(n.name), n.kind, sphere) for n in nodes],
            )

    def sync_from_pages(self, pages: list[Page]) -> None:
        pages = [
            replace(p, name=_nfc(p.name), aliases=tuple(_nfc(a) for a in p.aliases))
            for p in pages
        ]
        names: set[str] = set()
        for page in pages:
            _validate(page.name, page.kind)
            if _key(page.name) in names or any(not a.strip() for a in page.aliases):
                raise ValueError("duplicate page or empty alias")
            names.add(_key(page.name))
        with self._writing(
            page_origins={_key(p.name): p.source_ids for p in pages}
        ) as (db, _, edits):
            db.execute("DELETE FROM wiki_alias")
            db.execute("DELETE FROM wiki")
            for page in pages:
                db.execute(
                    "INSERT INTO wiki VALUES (?, ?, ?)",
                    (page.name, page.kind, page.summary),
                )
                db.executemany(
                    "INSERT INTO wiki_alias VALUES (?, ?)",
                    [(page.name, a) for a in set(page.aliases)],
                )
            for key, edit in edits.items():
                if edit.origin_wiki and not any(_key(n) in names for n in edit.bases):
                    db.execute("DELETE FROM edited WHERE name=?", (key,))
            # Old hidden rows are obsolete: hiding now depends on live aliases.
            db.execute("DELETE FROM hidden")
