import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from weaver.wiki.store import HASH_KEY, HumanEdit, HumanEditPending, WikiStore


def test_write_guards_and_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [10000.0]
    store = WikiStore(tmp_path / "vault", tmp_path / "state", clock=lambda: now[0])
    rev = store.write("page.md", {"title": "hello"}, "one", None)
    path = store.root / "page.md"
    assert f"{HASH_KEY}:" in path.read_text()
    with pytest.raises(HumanEditPending):
        store.write("page.md", {}, "two", "wrong")
    with pytest.raises(HumanEditPending):
        store.write("page.md", {}, "two", rev)
    os.utime(path, (now[0], now[0]))
    now[0] += 1801
    monkeypatch.setattr(store, "_before_replace", lambda: path.write_text("human edit"))
    with pytest.raises(HumanEditPending):
        store.write("page.md", {}, "two", rev)
    assert path.read_text() == "human edit"


def test_human_stability_delivery_and_authors(tmp_path: Path) -> None:
    now = [10000.0]
    store = WikiStore(tmp_path / "vault", tmp_path / "state", clock=lambda: now[0])
    store.write("page.md", {"title": "old"}, "original", None)
    path = store.root / "page.md"
    os.utime(path, (now[0], now[0]))
    now[0] += 1801
    os.utime(path, (now[0], now[0]))
    assert store.scan(now[0]) == []
    path.write_text('---\ntitle: "new"\n---\nfirst')
    os.utime(path, (now[0], now[0]))
    assert store.scan(now[0]) == []
    now[0] += 1000
    path.write_text('---\ntitle: "new"\n---\nfinal')
    os.utime(path, (now[0], now[0]))
    assert store.scan(now[0]) == []
    now[0] += 1801
    edits = store.scan(now[0])
    assert len(edits) == 1
    assert "original" in edits[0].diff and "final" in edits[0].diff
    assert "first" not in edits[0].diff
    assert edits[0].frontmatter_before["title"] == "old"
    assert edits[0].frontmatter_after["title"] == "new"
    reopened = WikiStore(tmp_path / "vault", tmp_path / "state", clock=lambda: now[0])
    assert reopened.scan(now[0]) == edits
    reopened.acknowledge(edits[0])
    assert reopened.scan(now[0]) == []
    authors = (
        subprocess.check_output(
            ["git", "--git-dir", str(store.git_dir), "log", "--format=%an"]
        )
        .decode()
        .splitlines()
    )
    assert authors[:2] == ["human", "weaver"]
    assert not (store.root / ".git").exists()


def test_readonly_restore_and_paths(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "vault", tmp_path / "state")
    store.write("copy.md", {}, "original", None, readonly=True)
    path = store.root / "copy.md"
    path.write_text("changed")
    store.restore_readonly()
    assert "original" in path.read_text()
    assert store.scan(9999999999) == []
    with pytest.raises(ValueError):
        store.write("../outside", {}, "x", None)
    (store.root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        store.write("link/escape", {}, "x", None)


def test_new_human_page_and_ignored_paths(tmp_path: Path) -> None:
    now = 10000.0
    store = WikiStore(tmp_path / "vault", tmp_path / "state", clock=lambda: now)
    (store.root / "human.md").write_text("hello")
    os.utime(store.root / "human.md", (now, now))
    (store.root / ".hidden").write_text("ignored")
    (store.root / "ignored.tmp").write_text("ignored")
    (store.root / "link").symlink_to(tmp_path, target_is_directory=True)
    assert store.scan(now) == []
    edits = store.scan(now + 1801)
    assert len(edits) == 1 and edits[0].path == "human.md"
    assert "+hello" in edits[0].diff
    with pytest.raises(ValueError):
        WikiStore(tmp_path / "vault", tmp_path / "other", wiki_root="../escape")


@pytest.mark.parametrize("suffix", [".png", ".apk", ".txt", ".json", ".csv", ".MD"])
def test_scan_silently_ignores_non_markdown(
    tmp_path: Path, caplog, suffix: str
) -> None:
    now = 10000.0
    store = WikiStore(tmp_path / "vault", tmp_path / "state", clock=lambda: now)
    folder = store.root / "attachments.md"
    folder.mkdir()
    path = folder / f"attachment{suffix}"
    data = b"\x89PNG\xff" if suffix in (".png", ".apk") else b'{"synthetic": true}'
    path.write_bytes(data)
    os.utime(path, (now, now))
    head = store._git("rev-parse", "HEAD")

    assert store.scan(now) == []
    assert store.scan(now + 1801) == []
    with store._db() as db:
        assert db.execute("SELECT path FROM pages").fetchall() == []
        assert db.execute("SELECT * FROM human_edits").fetchall() == []
    assert store._git("rev-parse", "HEAD") == head
    assert path.read_bytes() == data
    assert caplog.records == []


@pytest.mark.parametrize("readonly", [False, True])
def test_startup_removes_legacy_non_markdown_rows_only(tmp_path: Path, readonly: bool):
    store = WikiStore(tmp_path / "vault", tmp_path / "state")
    store.write("kept.md", {}, "synthetic markdown", None)
    (store.root / "kept.md").unlink()
    head = store._git("rev-parse", "HEAD")
    for name in (
        "image.png",
        "app.apk",
        "notes.txt",
        "data.json",
        "upper.MD",
        "missing.txt",
    ):
        if name != "missing.txt":
            (store.root / name).write_bytes(b"synthetic attachment")
        # Include a historical git entry to prove migration never removes it.
        head = store._commit(name, b"synthetic attachment", "human", "legacy file")
        with store._db() as db:
            db.execute(
                "INSERT INTO pages VALUES (?, ?, ?, ?, 'candidate', 100, 100)",
                (name, b"", head, int(readonly)),
            )
    with store._db() as db:
        markdown_row = db.execute("SELECT * FROM pages WHERE path='kept.md'").fetchone()
    tree = store._git("ls-tree", "-r", "HEAD")

    for _ in range(2):
        store = WikiStore(tmp_path / "vault", tmp_path / "state")
        with store._db() as db:
            assert db.execute("SELECT * FROM pages").fetchall() == [markdown_row]
        store.restore_readonly()
        assert store.scan(10000) == []
        assert store.scan(11801) == []
        assert store._git("rev-parse", "HEAD") == head
        assert store._git("ls-tree", "-r", "HEAD") == tree
        for name in ("image.png", "app.apk", "notes.txt", "data.json", "upper.MD"):
            assert (store.root / name).read_bytes() == b"synthetic attachment"
        assert not (store.root / "missing.txt").exists()
        assert not (store.root / "kept.md").exists()


def test_startup_preserves_only_markdown_edit_delivery_and_history(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state")
    edits = [
        HumanEdit(path, "base", path, "synthetic diff", {}, {})
        for path in ("kept.md", "notes.txt", "data.json", "upper.MD")
    ]
    with store._db() as db:
        for edit in edits:
            payload = json.dumps(asdict(edit))
            db.execute("INSERT INTO pending VALUES (?, ?)", (edit.new_rev, payload))
            db.execute("INSERT INTO human_edits VALUES (?, ?)", (edit.new_rev, payload))

    for _ in range(2):
        store = WikiStore(tmp_path / "vault", tmp_path / "state")
        assert store.scan(10000) == [edits[0]]
        assert [state.path for state in store.edit_states()] == ["kept.md"]
        with store._db() as db:
            assert db.execute("SELECT rev FROM pending").fetchall() == [("kept.md",)]
            assert db.execute("SELECT rev FROM human_edits").fetchall() == [
                ("kept.md",)
            ]


def test_scan_detects_markdown_beside_ignored_files_and_in_subfolders(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state")
    for directory in (store.root, store.root / "nested"):
        directory.mkdir(exist_ok=True)
        for name in ("page.md", "image.png", "notes.txt", "data.json", "upper.MD"):
            path = directory / name
            path.write_text("synthetic content")
            os.utime(path, (10000, 10000))

    assert store.scan(10000) == []
    edits = store.scan(11801)
    assert {edit.path for edit in edits} == {"page.md", "nested/page.md"}
    assert all("+synthetic content" in edit.diff for edit in edits)
    with store._db() as db:
        assert {row[0] for row in db.execute("SELECT path FROM pages")} == {
            "page.md",
            "nested/page.md",
        }


@pytest.mark.parametrize("digest", ["0b" + "a" * 62, "0e" + "1" * 62, "1" * 64])
def test_internal_hash_remains_text_even_when_it_looks_numeric(tmp_path, digest):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    page = store.root / "hash.md"
    page.write_text(f"---\n{HASH_KEY}: {digest}\n---\n본문\n")
    props, body, _ = store.read("hash.md")
    assert props[HASH_KEY] == digest
    assert body == "본문\n"
