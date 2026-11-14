"""macOS enumerates Korean filenames in decomposed Unicode form."""

import unicodedata

from weaver.wiki.store import WikiStore


def test_ai_page_is_not_rediscovered_as_human_when_walk_decomposes_name(
    tmp_path, monkeypatch
):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    path = "pages/계정 잠금.md"
    store.write(path, {}, "AI 기록", None)
    filename = unicodedata.normalize("NFD", "계정 잠금.md")
    monkeypatch.setattr(
        "weaver.wiki.store.os.walk",
        lambda *args, **kwargs: [(str(store.root / "pages"), [], [filename])],
    )
    assert store.scan(2_000_000_000) == []
    assert store.scan(2_000_000_001) == []
    with store._db() as db:
        assert db.execute("SELECT path FROM pages").fetchall() == [(path,)]


def test_path_aliases_use_same_tracked_version_and_restore_readonly(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    path = "조회/열린 요청.md"
    alias = unicodedata.normalize("NFD", path)
    digest = store.write(alias, {}, "원문", None, readonly=True)
    assert store.is_tracked_version(path, digest)
    assert store.is_tracked_version(alias, digest)
    (store.root / path).write_text("편집")
    store.restore_readonly()
    assert store.read(path)[1] == "원문\n"


def test_migration_removes_empty_shadow_but_preserves_real_baseline(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    path = "pages/계정 잠금.md"
    digest = store.write(path, {}, "기록", None)
    with store._db() as db:
        db.execute(
            "INSERT INTO pages VALUES (?, ?, ?, 0, 'shadow', 100, NULL)",
            (unicodedata.normalize("NFD", path), b"", "old"),
        )
    restarted = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    assert restarted.is_tracked_version(path, digest)
    assert restarted.scan(2000) == []
    with restarted._db() as db:
        assert db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1
