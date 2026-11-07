"""Atomic monthly reservations and redacted call records, shared across clients.

Integers are nano-USD so reservations never lose cents to float rounding.
A crash leaves its reservation intact. Unknown billed usage is never zeroed.
"""

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import BudgetExceeded

logger = logging.getLogger(__name__)
NANO_USD = 1_000_000_000


class Ledger:
    def __init__(
        self, path: Path, *, warn_usd: float = 15, stop_usd: float = 20
    ) -> None:
        if not 0 <= warn_usd <= stop_usd:
            raise ValueError("invalid budget limits")
        self.path = path
        self.warn_usd = warn_usd
        self.stop_usd = stop_usd
        # Parent is the explicit state_root; never create user vault paths.
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS llm_calls (
                id TEXT PRIMARY KEY, month TEXT NOT NULL,
                charged_nano INTEGER NOT NULL, record TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS llm_month ON llm_calls(month)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, record: dict[str, Any], amount: int) -> str:
        call_id = uuid4().hex
        month = datetime.now(UTC).strftime("%Y-%m")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            total = int(
                db.execute(
                    "SELECT COALESCE(SUM(charged_nano), 0) FROM llm_calls WHERE month=?",
                    (month,),
                ).fetchone()[0]
            )
            blocked = total + amount > round(self.stop_usd * NANO_USD)
            if blocked:
                record["status"] = BudgetExceeded.code
            held = 0 if blocked else amount
            record["budget_held_usd"] = held / NANO_USD
            db.execute(
                "INSERT INTO llm_calls VALUES (?, ?, ?, ?)",
                (
                    call_id,
                    month,
                    held,
                    json.dumps(record),
                ),
            )
        if blocked:
            raise BudgetExceeded()
        if total + amount >= round(self.warn_usd * NANO_USD):
            # Only fixed text: no prompt, credential, provider body or task value.
            logger.warning("LLM monthly budget warning threshold reached")
        return call_id

    def finish(self, call_id: str, record: dict[str, Any], charge: int) -> None:
        record["budget_held_usd"] = charge / NANO_USD
        with self._connect() as db:
            db.execute(
                "UPDATE llm_calls SET charged_nano=?, record=? WHERE id=?",
                (
                    charge,
                    json.dumps(record),
                    call_id,
                ),
            )

    def records(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT record FROM llm_calls ORDER BY rowid")
            ]
