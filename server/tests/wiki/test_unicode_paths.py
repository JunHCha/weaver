"""Unicode paths must have one identity regardless of filesystem spelling."""

import json
import os
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from weaver.wiki.store import WikiStore

NFC = "한글/계정잠금.md"
NFD = unicodedata.normalize("NFD", NFC)


def test_nfd_file_write_and_real_human_edit(tmp_path: Path) -> None:
    now = [10000.0]
    store = WikiStore(tmp_path / "vault", tmp_path / "state", clock=lambda: now[0])
    disk = store.root / NFD
    disk.parent.mkdir()
    disk.write_text("original")
    os.utime(disk, (now[0] - 1801, now[0] - 1801))
    base = store.read(NFD)[2]
    digest = store.write(NFC, {}, "compiled", base)
    assert store.is_tracked_version(NFD, digest)
    os.utime(disk, (now[0], now[0]))
    assert store.scan(now[0]) == []
    now[0] += 1801
    assert store.scan(now[0]) == []
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT path FROM pages").fetchall() == [(NFC,)]
    assert len(list(store.root.rglob("*.md"))) == 1
    assert store._git("ls-tree", "-rz", "--name-only", "HEAD") == NFC + "\0"

    disk.write_text("actual human change")
    os.utime(disk, (now[0], now[0]))
    assert store.scan(now[0]) == []
    now[0] += 1801
    edits = store.scan(now[0])
    assert len(edits) == 1
    assert edits[0].path == NFC
    assert "+actual human change" in edits[0].diff
    store.acknowledge(edits[0])
    assert store.scan(now[0] + 1801) == []


def test_nfd_write_uses_nfc_keys(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "vault", tmp_path / "state")
    digest = store.write(NFD, {}, "compiled", None)
    assert store.is_tracked_version(NFC, digest)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT path FROM pages").fetchall() == [(NFC,)]


def test_head_equal_repairs_baseline_without_commit(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "vault", tmp_path / "state")
    store.write("page.md", {}, "compiled\n\n", None)
    target = store.root / "page.md"
    data = target.read_bytes()
    head = store._git("rev-parse", "HEAD")
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE pages SET content=?", (b"stale baseline",))
    os.utime(target, (10000, 10000))
    assert store.scan(10000) == []
    assert store.scan(11801) == []
    assert store._git("rev-parse", "HEAD") == head
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT content, rev, mtime, candidate, since FROM pages"
        ).fetchone() == (data, head, 10000, None, None)
        assert db.execute("SELECT count(*) FROM pending").fetchone() == (0,)


@pytest.mark.parametrize("canonical_exists", [False, True])
def test_migrate_paths_idempotently(tmp_path: Path, canonical_exists: bool) -> None:
    vault, state = tmp_path / "vault", tmp_path / "state"
    store = WikiStore(vault, state)
    store.write(NFC, {}, "compiled", None)
    with sqlite3.connect(store.db_path) as db:
        original = db.execute("SELECT * FROM pages").fetchone()
        db.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (NFD, b"old", "legacy", 0, "candidate", 1, 2),
        )
        if not canonical_exists:
            db.execute("DELETE FROM pages WHERE path=?", (NFC,))
        db.execute(
            "INSERT INTO pending VALUES (?, ?)", ("pending", json.dumps({"path": NFD}))
        )
    for _ in range(2):
        store = WikiStore(vault, state)
        with sqlite3.connect(store.db_path) as db:
            rows = db.execute("SELECT * FROM pages").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == NFC
            assert rows[0][1:] == (
                original[1:]
                if canonical_exists
                else (b"old", "legacy", 0, "candidate", 1, 2)
            )
            assert (
                json.loads(db.execute("SELECT payload FROM pending").fetchone()[0])[
                    "path"
                ]
                == NFC
            )


def test_existing_decomposed_wiki_root_and_symlink_guard(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    root = vault / unicodedata.normalize("NFD", "위키")
    root.mkdir(parents=True)
    store = WikiStore(vault, tmp_path / "state", wiki_root="위키")
    assert store.root.samefile(root)
    assert len(list(vault.iterdir())) == 1
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / unicodedata.normalize("NFD", "연결")).symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        store.write("연결/escape.md", {}, "must not write", None)
    assert list(outside.iterdir()) == []
