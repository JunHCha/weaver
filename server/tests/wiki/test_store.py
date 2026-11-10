import os
import subprocess
from pathlib import Path

import pytest

from weaver.publish.notes import embedded_hash
from weaver.wiki.store import HumanEditPending, WikiStore


def test_write_guards_and_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [10000.0]
    store = WikiStore(tmp_path / "vault", tmp_path / "state", clock=lambda: now[0])
    rev = store.write("page.md", {"title": "hello"}, "one", None)
    path = store.root / "page.md"
    assert embedded_hash(path.read_bytes()) is not None
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
