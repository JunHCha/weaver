import json
from pathlib import Path

import pytest
from server.tests.reference.conftest import ConfigFactory, Origin, company_toml, git

from weaver.reference import (
    ReferenceIndex,
    find_references,
    fts5_trigram_available,
    sync_all,
)

ROUTER = (
    "---\ntags: [infra/network/router]\n---\n# Router\n라우터 펌웨어 업데이트 절차\n"
).encode()


def test_fts5_trigram_available() -> None:
    assert fts5_trigram_available() is True


def test_find_references_tag_first_then_fts(
    origin: Origin, make_config: ConfigFactory
) -> None:
    origin.commit(
        {
            "infra/network/router.md": ROUTER,
            "wiki/other.md": "# 다른 문서\n라우터 펌웨어 이야기\n".encode(),
            "wiki/third.md": "# 셋째\n펌웨어 목록\n".encode(),
        }
    )
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    hits = find_references(
        config.state_root, ["회사/infra/network"], "라우터 펌웨어", 3
    )
    assert 1 < len(hits) <= 3
    assert hits[0].match == "tag" and hits[0].path == "infra/network/router.md"
    assert all(h.match == "fts" for h in hits[1:])
    assert len({(h.sphere, h.path, h.ordinal) for h in hits}) == len(hits)
    short = find_references(config.state_root, ["회사/infra/network"], "ab")
    assert [h.match for h in short] == ["tag"]
    with pytest.raises(ValueError):
        find_references(config.state_root, [], "x", 0)
    with pytest.raises(ValueError):
        find_references(config.state_root, [], "x", 21)


def test_find_without_database(tmp_path: Path) -> None:
    assert find_references(tmp_path, ["a"], "query") == []


def test_secrets_never_reach_index(origin: Origin, make_config: ConfigFactory) -> None:
    origin.commit(
        {
            "keys/server.pem": b"-----BEGIN CERTIFICATE-----\nabc\n",
            "a.md": b"---\ntitle: sk-abcdefghijklmnopqrstuvwx\n---\n# A\napi_key = sk-abcdefghijklmnopqrstuvwx\n",
        }
    )
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    import sqlite3

    db = sqlite3.connect(config.state_root / "reference.sqlite3")
    assert (
        "sk-abc"
        not in (db.execute("SELECT group_concat(text) FROM chunks").fetchone()[0])
    )
    assert (
        "sk-abc"
        not in (
            db.execute("SELECT group_concat(frontmatter_json) FROM chunks").fetchone()[
                0
            ]
        )
    )
    assert [r[0] for r in db.execute("SELECT path FROM files")] == ["a.md"]
    mode = (config.state_root / "reference.sqlite3").stat().st_mode & 0o777
    assert mode == 0o600


def test_home_vault_excludes_weaver_and_dotdirs(make_config: ConfigFactory) -> None:
    config, _ = make_config(
        '[reference]\ndefault_sphere = "개인"\n[[reference.vault]]\nsphere = "개인"\nsource = "home"\n'
        'include = ["3. Resource"]\n'
    )
    root = config.vault_root
    for name in (
        "Weaver/x.md",
        ".obsidian/a.md",
        ".trash/b.md",
        "3. Resource/golf/swing.md",
        "other/y.md",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("# T\nbody text\n")
    [first] = sync_all(config)
    index = ReferenceIndex(config.state_root / "reference.sqlite3")
    assert index.known_paths("개인") == {"3. Resource/golf/swing.md"}
    assert first.indexed == 1
    [second] = sync_all(config)
    assert second.indexed == 0 and not second.full
    (root / "3. Resource/golf/swing.md").unlink()
    [third] = sync_all(config)
    assert third.removed == 1
    assert index.known_paths("개인") == set()


SECRET_TAG = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFla"


def _db_text(config) -> str:  # type: ignore[no-untyped-def]
    import sqlite3

    db = sqlite3.connect(config.state_root / "reference.sqlite3")
    parts: list[str] = []
    for (table,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        if table == "chunks_fts":  # external content: columns live in chunks
            continue
        for row in db.execute(f'SELECT * FROM "{table}"'):
            parts.extend(str(v) for v in row)
    return "\n".join(parts)


def test_secret_tags_and_links_never_reach_index(
    origin: Origin, make_config: ConfigFactory
) -> None:
    origin.commit(
        {
            "a.md": (
                f"---\ntags: [{SECRET_TAG}]\n---\n# A\n"
                "링크 [[sk-abcdefghijklmnopqrstuvwx]] 본문 #" + SECRET_TAG + "\n"
            ).encode()
        }
    )
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    text = _db_text(config)
    assert "sk-abcdefghijklmnopqrstuvwx" not in text
    assert SECRET_TAG not in text
    import sqlite3

    db = sqlite3.connect(config.state_root / "reference.sqlite3")
    for needle in ('"abcdefghijk"', '"Zm9vYmFyYm"'):
        assert not db.execute(
            "SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ?", (needle,)
        ).fetchall()


def test_query_syntax_is_inert(origin: Origin, make_config: ConfigFactory) -> None:
    origin.commit({"a.md": "# A\n라우터 펌웨어 설명\n".encode()})
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    for query in ('라우터" OR *', "NEAR( 라우터", "라우터 AND 펌웨어", '"', "*"):
        find_references(config.state_root, [], query)
    assert find_references(config.state_root, [], "라우터 AND 펌웨어")


def test_rename_leaves_only_new_path(
    origin: Origin, make_config: ConfigFactory
) -> None:
    origin.commit({"old.md": "# T\n이전 이름 본문\n".encode()})
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    git(origin.path, "mv", "old.md", "new.md")
    git(origin.path, "commit", "-m", "mv")
    [report] = sync_all(config)
    assert not report.full
    index = ReferenceIndex(config.state_root / "reference.sqlite3")
    assert index.known_paths("회사") == {"new.md"}


def test_home_sync_maps_para_and_leaves_archive_untagged(
    make_config: ConfigFactory,
) -> None:
    from weaver.context import ContextDictionary
    from weaver.reference import ReferenceIndex, find_references, sync_all

    config, _ = make_config(
        '[reference]\ndefault_sphere = "개인"\n[[reference.vault]]\nsphere = "개인"\nsource = "home"\n'
    )
    for name, body in {
        "3. Resource/golf/swing.md": "# Swing\n임팩트 연습 메모\n",
        "4. Archive/old.md": "# Old\n보관된 옛날 메모\n",
    }.items():
        target = config.vault_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    sync_all(config)
    index = ReferenceIndex(config.state_root / "reference.sqlite3")
    assert index.tree_tags("개인") == {"개인/golf"}
    names = {e.name for e in ContextDictionary(config.state_root).list_entries()}
    assert {"개인", "개인/golf", "개인/미분류"} <= names
    assert not any("Archive" in n or "old" in n for n in names)
    assert find_references(config.state_root, [], "보관된 옛날")


def test_home_include_paths_are_relative_to_the_include_prefix(
    make_config: ConfigFactory,
) -> None:
    config, _ = make_config(
        '[reference]\ndefault_sphere = "가족"\n[[reference.vault]]\nsphere = "가족"\nsource = "home"\n'
        'include = ["2. Area/가족"]\n'
    )
    root = config.vault_root
    for name in ("2. Area/가족/행사/x.md", "2. Area/가족/top.md"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("# T\nbody text\n")
    sync_all(config)
    import sqlite3

    db = sqlite3.connect(config.state_root / "reference.sqlite3")
    tags = {
        path: json.loads(raw)
        for path, raw in db.execute("SELECT path, tags_json FROM chunks")
    }
    assert tags == {
        "2. Area/가족/행사/x.md": ["가족/행사"],
        "2. Area/가족/top.md": ["가족"],
    }
    # incremental sync keeps the same mapping
    (root / "2. Area/가족/행사/y.md").write_text("# Y\nmore text\n")
    sync_all(config)
    row = db.execute(
        "SELECT tags_json FROM chunks WHERE path=?", ("2. Area/가족/행사/y.md",)
    ).fetchone()
    assert json.loads(row[0]) == ["가족/행사"]


def test_sync_without_trigram_support_is_a_clear_error(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import weaver.reference.sync as sync_module
    from weaver.reference import ReferenceSyncError

    config, _ = make_config(
        '[reference]\ndefault_sphere = "개인"\n[[reference.vault]]\nsphere = "개인"\nsource = "home"\n'
    )
    monkeypatch.setattr(sync_module, "fts5_trigram_available", lambda: False)
    with pytest.raises(ReferenceSyncError, match="FTS5"):
        sync_all(config)
