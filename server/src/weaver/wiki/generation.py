"""Durable bounded per-page generation cache, private to each consumer's state."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path


def fingerprint(value: object) -> str:
    """Hash all inputs that determine a generated result."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class GenerationCache:
    """Keep the latest validated result until its page write succeeds."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        with sqlite3.connect(state_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS generations "
                "(path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)"
            )

    def get(self, path: str, inputs: object, generate: Callable[[], str]) -> str:
        key = fingerprint(inputs)
        with sqlite3.connect(self.state_path) as db:
            row = db.execute(
                "SELECT result FROM generations WHERE path=? AND fingerprint=?",
                (path, key),
            ).fetchone()
        if row is not None:
            return str(row[0])
        result = generate()
        with sqlite3.connect(self.state_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO generations VALUES (?, ?, ?)",
                (path, key, result),
            )
        return result

    def discard(self, path: str) -> None:
        """Remove a result only after its write has completed."""
        with sqlite3.connect(self.state_path) as db:
            db.execute("DELETE FROM generations WHERE path=?", (path,))
