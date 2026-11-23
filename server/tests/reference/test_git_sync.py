from pathlib import Path

import pytest
from server.tests.reference.conftest import ConfigFactory, Origin, company_toml, git

from weaver.config import reference_clone_dir
from weaver.reference import (
    ReferenceIndex,
    ReferenceSyncError,
    SyncReport,
    find_references,
    sync_all,
)

ROUTER = (
    "---\ntags: [infra/network/router]\n---\n# Router\n라우터 펌웨어 업데이트 절차\n"
).encode()
NOTES = "# Notes\n운영 메모 내용\n".encode()


def seed(origin: Origin) -> str:
    return origin.commit({"infra/network/router.md": ROUTER, "ops/notes.md": NOTES})


def test_first_sync_clones_under_state_and_indexes(
    origin: Origin, make_config: ConfigFactory
) -> None:
    head = seed(origin)
    config, _ = make_config(company_toml(origin))
    [report] = sync_all(config)
    assert reference_clone_dir(config.state_root, "회사").is_dir()
    index = ReferenceIndex(config.state_root / "reference.sqlite3")
    assert {"회사/infra/network/router", "회사/ops"} <= index.tree_tags("회사")
    assert index.last_commit("회사") == head
    assert report.full and report.indexed == 2


def test_incremental_sync_uses_diff(origin: Origin, make_config: ConfigFactory) -> None:
    seed(origin)
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    origin.commit(
        {
            "infra/network/router.md": ROUTER + "추가 줄\n".encode(),
            "ops/notes.md": None,
            "new.md": b"# New\nbody text here\n",
        }
    )
    [report] = sync_all(config)
    assert report == SyncReport(
        "회사", origin.head(), full=False, indexed=2, removed=1, skipped=0
    )
    hits = find_references(config.state_root, ["회사/ops"], "운영 메모")
    assert all(h.path != "ops/notes.md" for h in hits)


def test_non_ancestor_commit_forces_full_reindex(
    origin: Origin, make_config: ConfigFactory
) -> None:
    seed(origin)
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    index = ReferenceIndex(config.state_root / "reference.sqlite3")
    index.remove("회사", ["ops/notes.md"])
    index.set_last_commit("회사", "0" * 40)
    [report] = sync_all(config)
    assert report.full
    assert "회사/ops" in index.tree_tags("회사")  # rebuilt from scratch


def test_clone_never_commits(origin: Origin, make_config: ConfigFactory) -> None:
    seed(origin)
    config, _ = make_config(company_toml(origin))
    clone = reference_clone_dir(config.state_root, "회사")
    before = git(origin.path, "status", "--porcelain")
    sync_all(config)
    origin.commit({"extra.md": b"# E\nx body\n"})
    sync_all(config)
    assert git(clone, "rev-list", "--count", "HEAD") == git(
        origin.path, "rev-list", "--count", "HEAD"
    )
    assert git(clone, "status", "--porcelain") == ""
    assert git(origin.path, "status", "--porcelain") == before == ""
    assert git(origin.path, "rev-parse", "HEAD") == origin.head()


def test_diverged_clone_raises(origin: Origin, make_config: ConfigFactory) -> None:
    seed(origin)
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    clone = reference_clone_dir(config.state_root, "회사")
    (clone / "local.md").write_text("# L\nlocal\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "local")
    with pytest.raises(ReferenceSyncError, match="reclone") as raised:
        sync_all(config)
    assert str(origin.path) not in str(raised.value)
    [report] = sync_all(config, reclone=True)
    assert report.full
    assert not (clone / "local.md").exists()


def test_error_never_contains_remote(
    tmp_path: Path, make_config: ConfigFactory
) -> None:
    secret = tmp_path / "token-abc" / "missing"
    toml = (
        '[[reference.vault]]\nsphere = "회사"\nsource = "git"\n'
        f'git_remote = "{secret}"\n'
    )
    config, _ = make_config(toml)
    with pytest.raises(ReferenceSyncError) as raised:
        sync_all(config)
    assert "token-abc" not in str(raised.value)


def test_sync_imports_tree_nodes(origin: Origin, make_config: ConfigFactory) -> None:
    from weaver.context import ContextDictionary

    seed(origin)
    config, _ = make_config(company_toml(origin))
    sync_all(config)
    snapshot = ContextDictionary(config.state_root).snapshot()
    network = snapshot.resolve("회사/infra/network")
    assert network is not None and network.kind == "area"
    assert snapshot.resolve("회사/미분류") is not None
