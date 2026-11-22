"""Read-only pipeline journal access; outside Courier (ADR-049).

The progress SQLite is the source. The Courier file projection is a separate,
rollback-only copy, so this model never reads or depends on it.
"""

import contextlib
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from weaver.runtime.pipeline_projection import pipeline_payload
from weaver.schema import PipelineItem

from .errors import ApiError
from .store import canonical, content_hash

logger = logging.getLogger("weaver.mobile")


@dataclass(frozen=True)
class PipelineEntry:
    source_id: str
    revision: int
    payload: dict[str, object]
    etag: str


class PipelineUnavailable(Exception):
    """The progress database cannot be read right now; the caller keeps its state."""


@dataclass(frozen=True)
class PipelineSnapshot:
    entries: list[PipelineEntry]
    by_id: dict[str, PipelineEntry]
    # Rows that failed validation. They exist, so a feed must not tombstone them.
    skipped: frozenset[str]


class PipelineReadModel:
    def __init__(self, state: Path) -> None:
        self.path = state / "pipeline.sqlite3"
        self._lock = threading.Lock()
        self._cache: tuple[tuple[int, ...], PipelineSnapshot] | None = None

    def available(self) -> bool:
        return self.path.is_file()

    def _key(self) -> tuple[int, ...]:
        key: list[int] = []
        for suffix in ("", "-wal"):
            try:
                info = os.stat(str(self.path) + suffix)
                key += [info.st_mtime_ns, info.st_size, info.st_ino]
            except OSError:
                key += [0, 0, 0]
        return tuple(key)

    def snapshot(self) -> PipelineSnapshot:
        """Items ordered by source_id; reparsed only when the database changed.

        One broken row is skipped (and logged by ID and error type, never its
        content); an unreadable database raises PipelineUnavailable.
        """
        with self._lock:
            key = self._key()
            if self._cache is not None and self._cache[0] == key:
                return self._cache[1]
            if not self.path.is_file():
                return PipelineSnapshot([], {}, frozenset())
            try:
                with contextlib.closing(
                    sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
                ) as db:
                    rows = db.execute(
                        "SELECT source_id, payload FROM items ORDER BY source_id"
                    ).fetchall()
            except (sqlite3.Error, OSError) as error:
                logger.warning("pipeline read failed: %s", type(error).__name__)
                raise PipelineUnavailable from error
            entries: list[PipelineEntry] = []
            skipped: set[str] = set()
            for source_id, raw in rows:
                try:
                    item = PipelineItem.model_validate_json(raw)
                    payload = pipeline_payload(item)
                except (ValidationError, ValueError, TypeError) as error:
                    skipped.add(str(source_id))
                    logger.warning(
                        "pipeline row skipped: %s %s",
                        source_id,
                        type(error).__name__,
                    )
                    continue
                entries.append(
                    PipelineEntry(
                        item.source_id,
                        item.revision,
                        payload,
                        content_hash(canonical(payload)),
                    )
                )
            snapshot = PipelineSnapshot(
                entries, {e.source_id: e for e in entries}, frozenset(skipped)
            )
            self._cache = (key, snapshot)
            return snapshot

    def _served(self) -> PipelineSnapshot:
        try:
            return self.snapshot()
        except PipelineUnavailable:
            raise ApiError(503, "pipeline_unavailable", retryable=True) from None

    def get(self, source_id: str) -> PipelineEntry:
        entry = self._served().by_id.get(source_id)
        if entry is None:
            raise ApiError(404, "pipeline_not_found")
        return entry

    def page(self, cursor: str | None, limit: int) -> dict[str, object]:
        entries = self._served().entries
        # The cursor pins the exact list it was cut from; any change restarts it.
        generation = content_hash(canonical([[e.source_id, e.etag] for e in entries]))
        offset = 0
        if cursor is not None:
            parts = cursor.split(":")
            if len(parts) != 2 or not parts[1].isdigit():
                raise ApiError(422, "invalid_cursor")
            offset = int(parts[1])
            if parts[0] != generation or offset > len(entries):
                raise ApiError(410, "cursor_expired")
        end = offset + limit
        return {
            "items": [e.payload for e in entries[offset:end]],
            "next_cursor": f"{generation}:{end}" if end < len(entries) else None,
        }
