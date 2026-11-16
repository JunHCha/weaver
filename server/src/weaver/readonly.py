"""SQLite reads that cannot create WAL/SHM or rollback-journal files.

Use for offline migration planning. An uncheckpointed writer must be stopped
before planning; immutable SQLite deliberately cannot consume its live WAL.
"""

import sqlite3
from pathlib import Path


def connect_snapshot(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    for suffix in ("-wal", "-journal"):
        journal = Path(str(path) + suffix)
        if journal.is_file() and journal.stat().st_size:
            raise ValueError(
                f"{path.name}: 활성 SQLite journal; 데몬/작성자를 종료하고 다시 계획 필요"
            )
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
