"""SQLite FTS5 (trigram) index of reference vault chunks."""

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import JsonValue

from weaver.llm.policy import mask_secrets
from weaver.reference.chunks import EXCLUDED_DIRS, indexable, parse
from weaver.wiki.store import normalize_path

DB_NAME = "reference.sqlite3"
MAX_LIMIT = 20
_TOKEN = re.compile(r"\w+")

Mapper = Callable[[Sequence[str], str], tuple[str, ...]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  sphere TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
  PRIMARY KEY(sphere, path));
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, sphere TEXT NOT NULL, path TEXT NOT NULL,
  heading TEXT NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL,
  tags_json TEXT NOT NULL, folder TEXT NOT NULL, links_json TEXT NOT NULL,
  frontmatter_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(sphere, path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  heading, text, tags, folder,
  content='chunks', content_rowid='id', tokenize='trigram');
CREATE TABLE IF NOT EXISTS vault_state(
  sphere TEXT PRIMARY KEY, last_commit TEXT, indexed_at TEXT NOT NULL);
"""


@dataclass(frozen=True)
class RefChunk:
    sphere: str
    path: str
    heading: str
    ordinal: int
    text: str
    tags: tuple[str, ...]
    folder: str
    links: tuple[str, ...]
    commit: str | None
    score: float
    match: Literal["tag", "fts"]


@dataclass(frozen=True)
class SyncReport:
    sphere: str
    commit: str | None
    full: bool
    indexed: int
    removed: int
    skipped: int


def fts5_trigram_available() -> bool:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
    except sqlite3.OperationalError:
        return False
    finally:
        db.close()
    return True


def walk(root: Path, wiki_folder: str | None, include: Sequence[str] = ()) -> list[str]:
    """Indexable Markdown paths under ``root`` (relative, symlinks not followed)."""
    prefixes = [normalize_path(p).strip("/") for p in include]
    found: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        base = Path(current).relative_to(root)
        for name in files:
            rel = (base / name).as_posix()
            key = normalize_path(rel)
            if not indexable(PurePosixPath(key), wiki_folder=wiki_folder):
                continue
            if prefixes and not any(
                key == p or key.startswith(p + "/") for p in prefixes
            ):
                continue
            found.append(rel)
    return sorted(found)


_WIKI_LINK = re.compile(r"\[\[([^\]\n]*)\]\]")


def _mask_links(text: str) -> str:
    """mask_secrets leaves ``[[...]]`` alone, but a link target can hold a secret."""
    return _WIKI_LINK.sub(lambda m: f"[[{mask_secrets(m.group(1))}]]", text)


def _mask_json(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [_mask_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _mask_json(v) for k, v in value.items()}
    return value


def _match_query(query: str) -> str:
    tokens = [
        t for t in _TOKEN.findall(unicodedata.normalize("NFC", query)) if len(t) >= 3
    ]
    return " OR ".join(f'"{t}"' for t in tokens)


def _depth(path: str) -> int:
    return path.count("/") + 1


class ReferenceIndex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        os.chmod(path, 0o600)
        self._path = path
        db = self._connect()
        try:
            db.executescript(_SCHEMA)
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, isolation_level=None, timeout=30)
        return db

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _delete(db: sqlite3.Connection, sphere: str, path: str) -> bool:
        rows = db.execute(
            "SELECT id, heading, text, tags_json, folder FROM chunks"
            " WHERE sphere=? AND path=?",
            (sphere, path),
        ).fetchall()
        for id_, heading, text, tags_json, folder in rows:
            db.execute(
                "INSERT INTO chunks_fts(chunks_fts, rowid, heading, text, tags, folder)"
                " VALUES('delete', ?, ?, ?, ?, ?)",
                (id_, heading, text, " ".join(json.loads(tags_json)), folder),
            )
        db.execute("DELETE FROM chunks WHERE sphere=? AND path=?", (sphere, path))
        cur = db.execute("DELETE FROM files WHERE sphere=? AND path=?", (sphere, path))
        return cur.rowcount > 0

    def index_files(
        self,
        sphere: str,
        root: Path,
        paths: Iterable[str] | None,
        *,
        commit: str | None,
        wiki_folder: str | None,
        mapper: Mapper,
        include: Sequence[str] = (),
    ) -> SyncReport:
        """Index ``paths`` (``None``: clear the sphere and walk ``root``).

        ``mapper(doc_tags, folder)`` returns the tree tags stored for a document.
        """
        full = paths is None
        names = walk(root, wiki_folder, include) if paths is None else list(paths)
        indexed = skipped = 0
        with self._txn() as db:
            if full:
                for (path,) in db.execute(
                    "SELECT path FROM files WHERE sphere=?", (sphere,)
                ).fetchall():
                    self._delete(db, sphere, path)
            for raw in names:
                key = normalize_path(raw)
                pure = PurePosixPath(key)
                file = root / raw
                if (
                    ".." in pure.parts
                    or pure.is_absolute()
                    or not indexable(pure, wiki_folder=wiki_folder)
                    or file.is_symlink()
                    or not file.is_file()
                ):
                    self._delete(db, sphere, key)
                    skipped += 1
                    continue
                data = file.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                known = db.execute(
                    "SELECT digest FROM files WHERE sphere=? AND path=?", (sphere, key)
                ).fetchone()
                if known and known[0] == digest:
                    continue
                doc = parse(key, data)
                self._delete(db, sphere, key)
                if doc is None:
                    skipped += 1
                    continue
                folder = pure.parent.as_posix() if pure.parent.parts else ""
                tags = mapper([mask_secrets(t) for t in doc.tags], folder)
                front = json.dumps(_mask_json(doc.frontmatter), ensure_ascii=False)
                for chunk in doc.chunks:
                    heading = _mask_links(chunk.heading)
                    body = _mask_links(chunk.text)
                    cur = db.execute(
                        "INSERT INTO chunks(sphere, path, heading, ordinal, text,"
                        " tags_json, folder, links_json, frontmatter_json)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            sphere,
                            key,
                            heading,
                            chunk.ordinal,
                            body,
                            json.dumps(tags, ensure_ascii=False),
                            folder,
                            json.dumps(
                                [mask_secrets(x) for x in doc.links], ensure_ascii=False
                            ),
                            front,
                        ),
                    )
                    db.execute(
                        "INSERT INTO chunks_fts(rowid, heading, text, tags, folder)"
                        " VALUES(?,?,?,?,?)",
                        (
                            cur.lastrowid,
                            heading,
                            body,
                            " ".join(tags),
                            folder,
                        ),
                    )
                db.execute(
                    "INSERT OR REPLACE INTO files VALUES(?,?,?)", (sphere, key, digest)
                )
                indexed += 1
            db.execute(
                "INSERT OR REPLACE INTO vault_state VALUES(?,?,datetime('now'))",
                (sphere, commit),
            )
        return SyncReport(sphere, commit, full, indexed, 0, skipped)

    def remove(self, sphere: str, paths: Iterable[str]) -> int:
        with self._txn() as db:
            return sum(self._delete(db, sphere, normalize_path(p)) for p in paths)

    def known_paths(self, sphere: str) -> set[str]:
        db = self._connect()
        try:
            return {
                p
                for (p,) in db.execute(
                    "SELECT path FROM files WHERE sphere=?", (sphere,)
                )
            }
        finally:
            db.close()

    def last_commit(self, sphere: str) -> str | None:
        db = self._connect()
        try:
            row = db.execute(
                "SELECT last_commit FROM vault_state WHERE sphere=?", (sphere,)
            ).fetchone()
        finally:
            db.close()
        return None if row is None else row[0]

    def set_last_commit(self, sphere: str, commit: str | None) -> None:
        """Test hook and repair tool: overwrite the stored commit."""
        with self._txn() as db:
            db.execute(
                "INSERT OR REPLACE INTO vault_state VALUES(?,?,datetime('now'))",
                (sphere, commit),
            )

    def tree_tags(self, sphere: str) -> set[str]:
        db = self._connect()
        try:
            return {
                v
                for (v,) in db.execute(
                    "SELECT DISTINCT t.value FROM chunks c, json_each(c.tags_json) t"
                    " WHERE c.sphere=?",
                    (sphere,),
                )
            }
        finally:
            db.close()

    def find_references(
        self, tags: Sequence[str], query: str, limit: int = 5
    ) -> list[RefChunk]:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be 1..{MAX_LIMIT}")
        wanted = [
            normalize_path(t).strip("/") for t in tags if normalize_path(t).strip("/")
        ]
        db = self._connect()
        try:
            found: dict[tuple[str, str, int], RefChunk] = {}
            tag_hits = self._by_tag(db, wanted)
            for chunk in tag_hits:
                found.setdefault((chunk.sphere, chunk.path, chunk.ordinal), chunk)
            match = _match_query(query)
            if match:
                for chunk in self._by_fts(db, match, limit + len(found)):
                    found.setdefault((chunk.sphere, chunk.path, chunk.ordinal), chunk)
        finally:
            db.close()
        return list(found.values())[:limit]

    _COLUMNS = (
        "c.sphere, c.path, c.heading, c.ordinal, c.text, c.tags_json, c.folder,"
        " c.links_json, (SELECT last_commit FROM vault_state v WHERE v.sphere=c.sphere)"
    )

    @staticmethod
    def _row(
        row: tuple[object, ...], score: float, kind: Literal["tag", "fts"]
    ) -> RefChunk:
        sphere, path, heading, ordinal, text, tags, folder, links, commit = row
        return RefChunk(
            str(sphere),
            str(path),
            str(heading),
            int(str(ordinal)),
            str(text),
            tuple(json.loads(str(tags))),
            str(folder),
            tuple(json.loads(str(links))),
            None if commit is None else str(commit),
            score,
            kind,
        )

    def _by_tag(self, db: sqlite3.Connection, wanted: list[str]) -> list[RefChunk]:
        if not wanted:
            return []
        rows = db.execute(
            f"SELECT {self._COLUMNS} FROM chunks c WHERE c.tags_json != '[]'"
        ).fetchall()
        ranked: list[tuple[int, str, int, RefChunk]] = []
        for row in rows:
            doc_tags = json.loads(row[5])
            best = max(
                (
                    _depth(p)
                    for p in wanted
                    if any(t == p or t.startswith(p + "/") for t in doc_tags)
                ),
                default=0,
            )
            if best:
                ranked.append(
                    (best, row[1], row[3], self._row(row, float(best), "tag"))
                )
        ranked.sort(key=lambda r: (-r[0], r[1], r[2]))
        return [r[3] for r in ranked]

    def _by_fts(self, db: sqlite3.Connection, match: str, limit: int) -> list[RefChunk]:
        rows = db.execute(
            f"SELECT {self._COLUMNS}, bm25(chunks_fts) AS rank FROM chunks_fts"
            " JOIN chunks c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY rank, c.path, c.ordinal LIMIT ?",
            (match, limit),
        ).fetchall()
        return [self._row(row[:9], -float(row[9]), "fts") for row in rows]


def find_references(
    state_root: Path, tags: Sequence[str], query: str, limit: int = 5
) -> list[RefChunk]:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_LIMIT}")
    path = state_root / DB_NAME
    if not path.exists():
        return []
    return ReferenceIndex(path).find_references(tags, query, limit)
