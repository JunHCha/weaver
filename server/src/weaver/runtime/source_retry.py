"""Select recovery targets separately from their reset step.

Call reset only while holding the resident server's InstanceLock. Selection
uses read-only connections and never constructs stores or runs migrations.
"""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from weaver.archive.store import INDEX_NAME


@dataclass(frozen=True)
class RetryTarget:
    source_id: str
    error_class: str | None
    captured_at: datetime


def _read(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def select_targets(
    state_root: Path,
    archive_root: Path,
    *,
    status: str = "failed",
    error_class: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    source_ids: tuple[str, ...] = (),
    limit: int | None = None,
) -> list[RetryTarget]:
    """Return eligible rows in archive arrival order; dates use captured_at.

    Any package revision protects its Source, regardless of review verdict.
    Missing runtime state means there is no recovery work to do.
    """
    if status not in {"failed", "retry", "waiting_llm", "done"}:
        raise ValueError("unknown status")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if since and until and since >= until:
        raise ValueError("--since must be earlier than --until")
    runtime = state_root / "runtime.sqlite3"
    if not runtime.exists():
        return []
    with closing(_read(runtime)) as db:
        rows = db.execute(
            "SELECT source_id, error_class FROM sources WHERE status=?"
            " AND status != 'done' AND (? IS NULL OR error_class=?)",
            (status, error_class, error_class),
        ).fetchall()
    eligible = dict(rows)
    if source_ids:
        eligible = {sid: error for sid, error in rows if sid in source_ids}
    summary = state_root / "summary.sqlite3"
    if summary.exists():
        with closing(_read(summary)) as db:
            for (raw,) in db.execute("SELECT package FROM packages"):
                eligible.pop(json.loads(raw)["source_id"], None)
    if not eligible:
        return []
    targets = []
    with closing(_read(archive_root / INDEX_NAME)) as db:
        for sid, raw in db.execute(
            "SELECT source_id, source_json FROM sources ORDER BY rowid"
        ):
            if sid not in eligible:
                continue
            captured = datetime.fromisoformat(json.loads(raw)["captured_at"])
            if (since and captured < since) or (until and captured >= until):
                continue
            targets.append(RetryTarget(sid, eligible[sid], captured))
            if limit is not None and len(targets) >= limit:
                break
    return targets


def reset_targets(state_root: Path, targets: list[RetryTarget]) -> int:
    """Reset runtime state in one transaction; caller holds InstanceLock.

    This is the recovery step to extend with explicit artifact cleanup later.
    It deliberately leaves Spinner, pipeline and review artifacts untouched.
    """
    if not targets:
        return 0
    with closing(sqlite3.connect(state_root / "runtime.sqlite3")) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA fullfsync=ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            changed = 0
            for target in targets:
                changed += db.execute(
                    "UPDATE sources SET status='retry', error_class=NULL, attempts=0,"
                    " contract_failures=0, timeout_failures=0, next_attempt_at=NULL,"
                    " updated_at=? WHERE source_id=? AND status != 'done'",
                    (datetime.now(UTC).isoformat(), target.source_id),
                ).rowcount
    return changed
