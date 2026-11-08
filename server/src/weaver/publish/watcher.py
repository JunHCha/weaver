"""Daily read-only output scan with durable, explicitly acknowledged delivery.

The state database belongs outside the vault. A scan transaction records pending
immutable Entry payloads before returning them; acknowledge advances a whole
output snapshot only after every Entry for it has been appended. Pending payloads
are replayed unchanged across days/restarts. SQLite serializes concurrent scans.
"""

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic import JsonValue, TypeAdapter

from weaver.archive.ulid import UlidGenerator
from weaver.fabric import Fabric
from weaver.schema import Entry
from weaver.schema.models import EntityId, EntryId, EntrySource

from .fields import OUTPUT_FIELDS, normalize_field
from .frontmatter import read_properties
from .notes import SCHEMA_VERSION
from .publisher import AiFolder, PublishState

logger = logging.getLogger(__name__)
_ids = UlidGenerator()
_entities = TypeAdapter(list[EntityId])
_entries = TypeAdapter(list[EntryId])
_sources = TypeAdapter(list[EntrySource])


class OutputWatcher(Protocol):
    def scan(self) -> list[Entry]: ...


@dataclass(frozen=True)
class Snapshot:
    note_id: str
    entities: list[str]
    values: dict[str, JsonValue]


def _snapshot(path: Path) -> Snapshot | None:
    props = read_properties(path)
    if not props:
        return None
    if not props.get("weaver_source_ids"):
        # Ordinary notes are ignored; a formerly tracked note is diagnosed too.
        if "weaver_note_id" in props:
            raise ValueError("missing source")
        return None
    if (
        type(props.get("weaver_schema_version")) is not int
        or props["weaver_schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError("unknown schema")
    note = props.get("weaver_note_id")
    if not isinstance(note, str):
        raise ValueError("missing note ID")
    TypeAdapter(EntityId | EntryId).validate_python(note)
    _sources.validate_python(props["weaver_source_ids"])
    entities = sorted(set(_entities.validate_python(props.get("weaver_entity_ids"))))
    if not entities:
        raise ValueError("missing entities")
    _entries.validate_python(props.get("weaver_entry_ids"))
    values: dict[str, JsonValue] = {
        key: normalize_field(key, props.get(key)) for key in OUTPUT_FIELDS
    }
    return Snapshot(note, entities, values)


def _event_id(output: str, field: str, old: JsonValue, new: JsonValue, rev: int) -> str:
    # rev distinguishes a later repetition A→B→A→B from a delivery retry.
    data = json.dumps([output, field, old, new, rev], sort_keys=True).encode()
    number = int.from_bytes(hashlib.sha256(data).digest()[:16], "big")
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "evt_" + "".join(alphabet[(number >> n) & 31] for n in range(125, -1, -5))


def _change_value(field: str, old: JsonValue, new: JsonValue) -> JsonValue:
    if field != "assignees" or new is None:
        return new
    assert isinstance(new, list)
    previous = old if isinstance(old, list) else []
    return {
        "add": [v for v in new if v not in previous],
        "remove": [v for v in previous if v not in new],
    }


class SqliteOutputWatcher:
    def __init__(
        self,
        folder: AiFolder,
        state_path: Path,
        *,
        timezone: ZoneInfo,
        manifest: PublishState,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.folder, self.path = folder, state_path.resolve()
        if self.path == folder.vault or folder.vault in self.path.parents:
            raise ValueError("watcher state must be outside vault")
        self.zone, self.manifest, self.clock = timezone, manifest, clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported watcher database version")
            db.execute(
                "CREATE TABLE IF NOT EXISTS registry ("
                "id TEXT PRIMARY KEY, note TEXT NOT NULL, path TEXT NOT NULL,"
                "active INTEGER NOT NULL, baseline TEXT NOT NULL,"
                "rev INTEGER NOT NULL DEFAULT 0, target TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending ("
                "entry_id TEXT PRIMARY KEY, output_id TEXT NOT NULL,"
                "payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS meta (day TEXT NOT NULL)")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def _discover(self) -> tuple[dict[str, Snapshot], set[str]]:
        found: dict[str, Snapshot] = {}
        present: set[str] = set()
        # The manifest defines Publisher-managed paths even if folder settings
        # changed. Stalled AI notes are excluded just like current AI notes.
        managed = {row.path for row in self.manifest.outputs().values()}
        for directory, dirs, files in os.walk(self.folder.vault, followlinks=False):
            base = Path(directory)
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith((".", "~"))
                and not (base / d).is_symlink()
                and not self.folder.inside(base / d)
            )
            for name in sorted(files):
                path = base / name
                if (
                    not name.endswith(".md")
                    or name.startswith((".", "~"))
                    or path.is_symlink()
                    or not path.is_file()
                ):
                    continue
                relative = path.relative_to(self.folder.vault).as_posix()
                if relative in managed or self.folder.inside(path):
                    continue
                present.add(relative)
                try:
                    snapshot = _snapshot(path)
                    if snapshot is not None:
                        found[relative] = snapshot
                except (ValueError, OSError) as error:
                    # Never log note text, values or arbitrary exception messages.
                    logger.warning("output skipped: %s", type(error).__name__)
        return found, present

    def scan(self) -> list[Entry]:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("watcher clock must be timezone-aware")
        day = now.astimezone(self.zone).date().isoformat()
        with self._connect() as db:
            last = db.execute("SELECT day FROM meta").fetchone()
            if last is None or day > last[0]:
                found, present = self._discover()
                self._compare(db, found, present, now)
                db.execute("DELETE FROM meta")
                db.execute("INSERT INTO meta VALUES (?)", (day,))
            return [
                Entry.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT payload FROM pending WHERE acknowledged=0 ORDER BY rowid"
                )
            ]

    def _compare(
        self,
        db: sqlite3.Connection,
        found: dict[str, Snapshot],
        present: set[str],
        now: datetime,
    ) -> None:
        rows = list(db.execute("SELECT * FROM registry ORDER BY rowid"))
        by_key = {(r["note"], r["path"]): r for r in rows}
        new = [
            (path, snap)
            for path, snap in found.items()
            if (snap.note_id, path) not in by_key
        ]
        missing = [r for r in rows if r["active"] and r["path"] not in present]
        for path, snap in found.items():
            row = by_key.get((snap.note_id, path))
            if row is None:
                candidates = [r for r in missing if r["note"] == snap.note_id]
                destinations = [p for p, s in new if s.note_id == snap.note_id]
                if len(candidates) == 1 and len(destinations) == 1:
                    row = candidates[0]
                    missing.remove(row)
                    db.execute(
                        "UPDATE registry SET path=? WHERE id=?", (path, row["id"])
                    )
                else:
                    db.execute(
                        "INSERT INTO registry(id,note,path,active,baseline) "
                        "VALUES (?,?,?,1,?)",
                        (_ids(), snap.note_id, path, json.dumps(snap.values)),
                    )
                    continue
            db.execute("UPDATE registry SET active=1 WHERE id=?", (row["id"],))
            if row["target"] is not None:
                continue  # Preserve pending batch before comparing later edits.
            old = json.loads(row["baseline"])
            changed = [key for key in OUTPUT_FIELDS if old[key] != snap.values[key]]
            if not changed:
                continue
            for field in changed:
                entry = Entry(
                    schema_version=1,
                    entry_id=_event_id(
                        row["id"], field, old[field], snap.values[field], row["rev"]
                    ),
                    entities=snap.entities,
                    kind="변경",
                    field=field,
                    value=_change_value(field, old[field], snap.values[field]),
                    occurred_at=now,
                    recorded_at=now,
                    source_id="output:" + row["id"],
                    actor="human",
                    supersedes=None,
                    retracts=None,
                    request=None,
                    resolves=None,
                    blocked_by_request=None,
                )
                db.execute(
                    "INSERT INTO pending(entry_id,output_id,payload) VALUES (?,?,?)",
                    (entry.entry_id, row["id"], entry.model_dump_json()),
                )
            db.execute(
                "UPDATE registry SET target=? WHERE id=?",
                (json.dumps(snap.values), row["id"]),
            )
        for row in missing:
            db.execute("UPDATE registry SET active=0 WHERE id=?", (row["id"],))

    def acknowledge(self, entry_ids: Sequence[str]) -> None:
        """Call only after Fabric.append succeeds (identical retries are safe).

        Partial confirmations are durable; the baseline advances atomically only
        once the entire output batch is confirmed. Unknown IDs reject the call.
        """
        with self._connect() as db:
            for entry_id in entry_ids:
                if (
                    db.execute(
                        "SELECT 1 FROM pending WHERE entry_id=?", (entry_id,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError("unknown output entry ID")
                db.execute(
                    "UPDATE pending SET acknowledged=1 WHERE entry_id=?", (entry_id,)
                )
            db.execute(
                "UPDATE registry SET baseline=target,target=NULL,rev=rev+1 "
                "WHERE target IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM pending WHERE output_id=registry.id AND acknowledged=0)"
            )


class OutputSync:
    """Human edits are facts (ADR-011); recording them grants no execution rights."""

    def __init__(self, watcher: SqliteOutputWatcher, fabric: Fabric) -> None:
        self.watcher, self.fabric = watcher, fabric

    def run(self) -> None:
        entries = self.watcher.scan()
        if entries:
            self.fabric.append(entries)
            self.watcher.acknowledge([e.entry_id for e in entries])
