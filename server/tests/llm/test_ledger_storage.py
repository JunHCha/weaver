"""Permissions and durability must not depend on the process umask."""

import os
import sqlite3
import stat

import pytest

from weaver.llm.ledger import Ledger


@pytest.mark.parametrize("mask", [0o022, 0o000])
def test_database_is_private_before_sqlite_opens_it(tmp_path, monkeypatch, mask):
    path = tmp_path / "llm.sqlite3"
    connect = sqlite3.connect
    modes_at_open = []

    def inspect_connect(*args, **kwargs):
        modes_at_open.append(
            stat.S_IMODE(path.stat().st_mode) if path.exists() else None
        )
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", inspect_connect)
    old = os.umask(mask)
    try:
        Ledger(path)
    finally:
        os.umask(old)
    assert modes_at_open == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reopen_repairs_database_and_wal_permissions_before_connect(
    tmp_path, monkeypatch
):
    path = tmp_path / "llm.sqlite3"
    ledger = Ledger(path)
    ledger.reserve({"status": "pending"}, 1234)
    external = sqlite3.connect(path)
    try:
        assert external.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        external.execute("CREATE TABLE fixture(value TEXT)")
        external.commit()
        paths = [
            path,
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
        ]
        for private in paths:
            private.chmod(0o644)
        connect = sqlite3.connect
        modes_at_open = []

        def inspect_connect(*args, **kwargs):
            modes_at_open.append([stat.S_IMODE(p.stat().st_mode) for p in paths])
            return connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", inspect_connect)
        Ledger(path)
        assert modes_at_open == [[0o600, 0o600, 0o600]]
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in paths)
        assert ledger.records()[0]["budget_held_usd"] == 1234 / 1_000_000_000
    finally:
        external.close()


def test_each_connection_enables_full_disk_flush(tmp_path):
    ledger = Ledger(tmp_path / "llm.sqlite3")
    for _ in range(2):
        with ledger._connect() as db:
            assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
            assert db.execute("PRAGMA fullfsync").fetchone()[0] == 1
            assert db.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
