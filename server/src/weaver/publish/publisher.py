"""Publisher: Fabric state into ID-named notes inside the vault's AI folder (#7).

Layout under ``vault_root/<publish.folder>`` (default ``Weaver``)::

    Projects/ent_<ULID>.md              project object note = MOC
    Entities/{People,Orgs,Topics,Meetings,Unknown}/ent_<ULID>.md
    Records/<YYYY-MM>/evt_<ULID>.md     one record note per Thread

Invariant first: a file Weaver did not write, or wrote but someone changed, is
never overwritten or deleted. Ownership is the embedded content hash (see
``notes``); a missing state database therefore never unlocks human files.
Nothing outside the AI folder is ever written, and nothing is ever deleted.
A note found by ID elsewhere inside the AI folder (moved, e.g. into an archive
subfolder) is updated in place instead of being duplicated.
"""

import json
import logging
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from zoneinfo import ZoneInfo

from weaver.courier.atomic import fsync_directory, write_bytes_atomic
from weaver.fabric import Fabric, ThreadQuery
from weaver.schema.internal import EntityIndex, IndexedEntity
from weaver.schema.models import ULID

from .notes import (
    SCHEMA_VERSION,
    Note,
    RelationLink,
    embedded_hash,
    entry_status,
    render_entity,
    render_entry,
    statuses,
)

logger = logging.getLogger(__name__)

_ENTITY_ID = re.compile(rf"^ent_{ULID}$")
TYPE_FOLDERS = {
    "project": "Projects",
    "person": "Entities/People",
    "org": "Entities/Orgs",
    "topic": "Entities/Topics",
    "meeting": "Entities/Meetings",
}
UNKNOWN_FOLDER = "Entities/Unknown"
# Notes are ordinary vault files (readable like the rest of the vault).
NOTE_MODE = 0o644


class PublishPathError(ValueError):
    """A target escaped the AI folder or the folder setting is unsafe."""


class EntityDirectory(Protocol):
    """Approved entity names/types (SqliteFeedbackStore.index, #27)."""

    def index(self) -> EntityIndex: ...


class RelationSource(Protocol):
    def relations(self) -> list[RelationLink]: ...


class NoRelations:
    def relations(self) -> list[RelationLink]:
        return []


@dataclass(frozen=True)
class Conflict:
    path: str  # relative to vault_root
    reason: str  # human_owned | duplicate_id | not_regular_file


def validate_folder(folder: str) -> PurePosixPath:
    """A relative, visible, non-traversing POSIX path such as ``Weaver``."""
    if not isinstance(folder, str) or not folder or "\\" in folder or "\0" in folder:
        raise PublishPathError("publish.folder: expected relative folder name")
    path = PurePosixPath(folder)
    if path.is_absolute() or any(
        part in ("", ".", "..") or part.startswith(".") for part in path.parts
    ):
        raise PublishPathError("publish.folder: expected relative folder name")
    return path


class AiFolder:
    """The only directory tree Publisher may write; symlinks are never followed."""

    def __init__(self, vault_root: Path, folder: str) -> None:
        self.vault = Path(os.path.realpath(vault_root))
        self.root = self.vault.joinpath(*validate_folder(folder).parts)

    def inside(self, path: Path) -> bool:
        real = Path(os.path.realpath(path))
        return real == self.root or self.root in real.parents

    def ensure_dir(self, relative: PurePosixPath) -> Path:
        directory = self.root.joinpath(*relative.parts)
        current = self.vault
        for part in directory.relative_to(self.vault).parts:
            current = current / part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise PublishPathError("publish: AI folder path is not a directory")
            current.mkdir(mode=0o755, exist_ok=True)
        if not self.inside(directory):
            raise PublishPathError("publish: path escapes the AI folder")
        return directory

    def scan(self) -> dict[str, list[Path]]:
        """Note filenames → paths inside the AI folder, dot entries skipped."""
        found: dict[str, list[Path]] = {}
        if not self.root.is_dir() or self.root.is_symlink():
            return found
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in files:
                if name.endswith(".md") and not name.startswith("."):
                    found.setdefault(name, []).append(Path(directory) / name)
        return found


# Manifest status values; anything but "current" means Weaver stopped updating.
CURRENT = "current"


@dataclass(frozen=True)
class OutputRow:
    path: str  # relative to vault_root
    sha256: str
    status: str


class PublishState:
    """state_root/publish.sqlite3: output manifest for OutputWatcher (#8) and
    the change cursor. Holds no note text; losing it only costs a full resync
    (ownership is decided by the embedded hash, never by this table).

    ``outputs.sha256`` is the note's ``weaver_content_sha256``: the SHA-256 of
    the file bytes with that one frontmatter line removed, not of the whole
    file. ``status`` is ``current`` or the Conflict reason that stopped updates
    (e.g. ``human_owned`` for an AI note a person edited), see ``stalled()``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS outputs (note_id TEXT PRIMARY KEY NOT NULL,"
                " kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,"
                " schema_version INTEGER NOT NULL, source_ids TEXT NOT NULL,"
                " entity_ids TEXT NOT NULL, entry_ids TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'current')"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY NOT NULL,"
                " value TEXT NOT NULL)"
            )
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def apply(
        self, records: list[tuple[Note, str]], stalled: list[tuple[str, str]]
    ) -> None:
        """One transaction per publish: written/confirmed rows, then stalls."""
        if not records and not stalled:
            return
        with self.connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO outputs VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        note.note_id,
                        note.kind,
                        path,
                        note.sha256,
                        SCHEMA_VERSION,
                        json.dumps(note.source_ids),
                        json.dumps(note.entity_ids),
                        json.dumps(note.entry_ids),
                        CURRENT,
                    )
                    for note, path in records
                ],
            )
            db.executemany(
                "UPDATE outputs SET status=? WHERE note_id=?",
                [(reason, note_id) for note_id, reason in stalled],
            )

    def outputs(self) -> dict[str, OutputRow]:
        """note_id → last known output (vault-relative path, sha256, status)."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT note_id, path, sha256, status FROM outputs"
            ).fetchall()
        return {row[0]: OutputRow(row[1], row[2], row[3]) for row in rows}

    def stalled(self) -> dict[str, OutputRow]:
        """AI notes Weaver no longer updates, e.g. ones a person edited (#8)."""
        return {k: v for k, v in self.outputs().items() if v.status != CURRENT}

    def get(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


class Publisher:
    """``publish(changed)`` rewrites only the changed entities' object notes and
    their record notes, and returns the paths it actually wrote.
    """

    def __init__(
        self,
        folder: AiFolder,
        fabric: Fabric,
        entities: EntityDirectory,
        *,
        relations: RelationSource | None = None,
        state: PublishState | None = None,
        timezone: ZoneInfo,
    ) -> None:
        self.folder = folder
        self.fabric = fabric
        self.entities = entities
        self.relations = relations or NoRelations()
        self.state = state
        self.zone = timezone
        self.conflicts: list[Conflict] = []
        self._known: dict[str, OutputRow] = {}
        self._records: list[tuple[Note, str]] = []
        self._stalled: list[tuple[str, str]] = []

    def publish(self, changed: list[str]) -> list[Path]:
        ids = list(dict.fromkeys(changed))
        if any(not isinstance(i, str) or not _ENTITY_ID.fullmatch(i) for i in ids):
            raise ValueError("changed must contain ent_ IDs only")
        self.conflicts = []
        if not ids:
            return []
        names = {e.entity_id: e for e in self.entities.index().entities}
        relations = self.relations.relations()
        existing = self.folder.scan()
        self._known = self.state.outputs() if self.state is not None else {}
        self._records, self._stalled = [], []
        written: list[Path] = []
        done: set[str] = set()
        try:
            for entity_id in ids:
                entries = self.fabric.entries(ThreadQuery(entity_id=entity_id))
                mine = [r for r in relations if entity_id in (*r.from_ids, *r.to_ids)]
                note = render_entity(
                    entity_id,
                    self.fabric.state(entity_id),
                    entries,
                    mine,
                    names,
                    self.zone,
                )
                directory = self._entity_dir(entity_id, names)
                self._write(note, directory, existing, written)
                # Fabric rejects supersedes/retracts across different entity
                # sets (entity_mismatch), so every entity of a record sees the
                # same edges and this status does not depend on publish order.
                retracted, superseded = statuses(entries)
                for item in entries:
                    if item.entry_id in done:
                        continue
                    done.add(item.entry_id)
                    status = entry_status(item, retracted, superseded)
                    month = item.occurred_at.astimezone(self.zone).strftime("%Y-%m")
                    self._write(
                        render_entry(item, status, names, self.zone),
                        PurePosixPath("Records", month),
                        existing,
                        written,
                    )
        finally:
            if self.state is not None:
                self.state.apply(self._records, self._stalled)
        return written

    @staticmethod
    def _entity_dir(
        entity_id: str, names: Mapping[str, IndexedEntity]
    ) -> PurePosixPath:
        known = names.get(entity_id)
        return PurePosixPath(
            TYPE_FOLDERS.get(known.type, UNKNOWN_FOLDER) if known else UNKNOWN_FOLDER
        )

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.folder.vault).as_posix()

    def _conflict(self, note_id: str, path: Path, reason: str) -> None:
        relative = self._relative(path)
        self.conflicts.append(Conflict(relative, reason))
        known = self._known.get(note_id)
        if known is not None and known.status != reason:
            self._stalled.append((note_id, reason))
        logger.warning("publish skipped %s: %s", relative, reason)

    def _relocate(self, note_id: str, old: Path, canonical: Path) -> Path:
        """Move a note Weaver itself placed to its new canonical folder (e.g.
        after an entity type change). A rename, never a delete; anything a
        person moved or edited stays where it is.
        """
        known = self._known.get(note_id)
        if (
            known is None
            or known.path != self._relative(old)
            or old.is_symlink()
            or not old.is_file()
            or canonical.exists()
            or canonical.is_symlink()
            or embedded_hash(old.read_bytes()) is None
        ):
            return old
        os.rename(old, canonical)
        fsync_directory(old.parent)
        fsync_directory(canonical.parent)
        logger.info(
            "publish moved %s -> %s", self._relative(old), self._relative(canonical)
        )
        return canonical

    def _write(
        self,
        note: Note,
        directory: PurePosixPath,
        existing: dict[str, list[Path]],
        written: list[Path],
    ) -> None:
        name = f"{note.note_id}.md"
        canonical = self.folder.root.joinpath(*directory.parts, name)
        found = existing.get(name, [])
        if canonical in found or not found:
            target = canonical
        elif len(found) == 1:
            if not self.folder.inside(found[0].parent):
                raise PublishPathError("publish: path escapes the AI folder")
            self.folder.ensure_dir(directory)
            # Moved by a person inside the AI folder: keep its place.
            target = self._relocate(note.note_id, found[0], canonical)
        else:
            self._conflict(note.note_id, found[0], "duplicate_id")
            return
        if target == canonical:
            self.folder.ensure_dir(directory)
        elif not self.folder.inside(target.parent):
            raise PublishPathError("publish: path escapes the AI folder")
        if target.is_symlink() or (target.exists() and not target.is_file()):
            self._conflict(note.note_id, target, "not_regular_file")
            return
        mode = NOTE_MODE
        if target.exists():
            current = target.read_bytes()
            if current == note.data:
                self._record(note, target)
                return  # unchanged: never rewrite
            if embedded_hash(current) is None:
                self._conflict(note.note_id, target, "human_owned")
                return
            mode = stat.S_IMODE(target.stat().st_mode)
        write_bytes_atomic(target, note.data, mode=mode)
        existing[name] = [target]
        self._record(note, target)
        written.append(target)

    def _record(self, note: Note, target: Path) -> None:
        relative = self._relative(target)
        known = self._known.get(note.note_id)
        if known == OutputRow(relative, note.sha256, CURRENT):
            return  # nothing changed: no manifest write
        self._records.append((note, relative))
        self._known[note.note_id] = OutputRow(relative, note.sha256, CURRENT)
