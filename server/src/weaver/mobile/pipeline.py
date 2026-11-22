"""Read-only pipeline journal access; outside Courier (ADR-049).

The progress SQLite is the source. The Courier file projection is a separate,
rollback-only copy, so this model never reads or depends on it.
"""

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from weaver.runtime.pipeline_projection import pipeline_payload
from weaver.schema import PipelineItem

from .errors import ApiError
from .store import canonical, content_hash


@dataclass(frozen=True)
class PipelineEntry:
    source_id: str
    revision: int
    payload: dict[str, object]
    etag: str


class PipelineReadModel:
    def __init__(self, state: Path) -> None:
        self.path = state / "pipeline.sqlite3"
        self._lock = threading.Lock()
        self._cache: tuple[tuple[int, ...], list[PipelineEntry]] | None = None

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

    def entries(self) -> list[PipelineEntry]:
        """Items ordered by source_id; reparsed only when the database changed."""
        with self._lock:
            key = self._key()
            if self._cache is not None and self._cache[0] == key:
                return self._cache[1]
            if not self.path.is_file():
                return []
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute(
                    "SELECT payload FROM items ORDER BY source_id"
                ).fetchall()
            entries = []
            for (raw,) in rows:
                item = PipelineItem.model_validate_json(raw)
                payload = pipeline_payload(item)
                entries.append(
                    PipelineEntry(
                        item.source_id,
                        item.revision,
                        payload,
                        content_hash(canonical(payload)),
                    )
                )
            self._cache = (key, entries)
            return entries

    def get(self, source_id: str) -> PipelineEntry:
        for entry in self.entries():
            if entry.source_id == source_id:
                return entry
        raise ApiError(404, "pipeline_not_found")

    def page(self, cursor: str | None, limit: int) -> dict[str, object]:
        entries = self.entries()
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
