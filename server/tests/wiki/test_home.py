import subprocess

import pytest

from weaver.wiki.home import (
    VaultNotIgnored,
    WikiBindingChanged,
    check_vault_ignored,
    rebind,
)
from weaver.wiki.store import WikiStore


def _git_init(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_git_vault_requires_ignore(tmp_path):
    home = tmp_path / "home"
    _git_init(home)
    with pytest.raises(VaultNotIgnored, match="Weaver/"):
        check_vault_ignored(home, "Weaver")
    (home / ".gitignore").write_text("Weaver/\n")
    check_vault_ignored(home, "Weaver")
    (home / ".gitignore").write_text("/Weaver\n")
    check_vault_ignored(home, "Weaver")


def test_non_git_vault_passes(tmp_path):
    (tmp_path / "plain").mkdir()
    check_vault_ignored(tmp_path / "plain", "Weaver")


def test_gitignore_fallback_without_git(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _git_init(home)

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(VaultNotIgnored):
        check_vault_ignored(home, "Weaver")
    (home / ".gitignore").write_text("# c\n/Weaver/**\n")
    check_vault_ignored(home, "Weaver")


def test_binding_change_requires_rebind(tmp_path):
    state, backup = tmp_path / "state", tmp_path / "backup"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    store = WikiStore(a, state)
    store.write("pages/x.md", {}, "본문", None)
    WikiStore(a, state)  # same location reopens
    with pytest.raises(WikiBindingChanged, match="wiki-rebind"):
        WikiStore(b, state)
    moved = rebind(state, backup)
    assert "wiki.git" in moved
    assert (backup / "wiki.git").exists()
    fresh = WikiStore(b, state)
    assert not (fresh.root / "pages/x.md").exists()


def test_unbound_state_adopts_only_when_pages_exist(tmp_path):
    import sqlite3

    state = tmp_path / "state"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    WikiStore(a, state).write("pages/x.md", {}, "본문", None)
    with sqlite3.connect(state / "wiki.sqlite3") as db:
        db.execute("DROP TABLE binding")
    WikiStore(a, state)  # legacy state adopted
    with sqlite3.connect(state / "wiki.sqlite3") as db:
        db.execute("DELETE FROM binding")
    with pytest.raises(WikiBindingChanged):
        WikiStore(b, state)


def test_wiki_rebind_command(tmp_path):
    from typer.testing import CliRunner

    from weaver.app import cli

    config = tmp_path / "server.toml"
    config.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    from weaver.config import load_config

    settings = load_config(config, init_directories=True)
    (settings.state_root / "wiki.sqlite3").write_text("")
    runner = CliRunner()
    backup = tmp_path / "bk"
    refused = runner.invoke(
        cli, ["wiki-rebind", "--config", str(config), "--backup", str(backup)]
    )
    assert refused.exit_code == 2 and not backup.exists()
    done = runner.invoke(
        cli, ["wiki-rebind", "--config", str(config), "--backup", str(backup), "--yes"]
    )
    assert done.exit_code == 0, done.output
    assert (backup / "wiki.sqlite3").exists()
    assert not (settings.state_root / "wiki.sqlite3").exists()


def test_gitignore_negation_rejected_without_git(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _git_init(home)

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    (home / ".gitignore").write_text("Weaver/\n!Weaver/\n")
    with pytest.raises(VaultNotIgnored):
        check_vault_ignored(home, "Weaver")


def test_wiki_rebind_refuses_backup_inside_vault(tmp_path):
    from typer.testing import CliRunner

    from weaver.app import cli
    from weaver.config import ConfigError, load_config

    config = tmp_path / "server.toml"
    config.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    settings = load_config(config, init_directories=True)
    (settings.state_root / "wiki.sqlite3").write_text("")
    inside = settings.vault_root / "bk"
    result = CliRunner().invoke(
        cli, ["wiki-rebind", "--config", str(config), "--backup", str(inside), "--yes"]
    )
    assert result.exit_code != 0 and not inside.exists()
    assert (settings.state_root / "wiki.sqlite3").exists()
    with pytest.raises(ConfigError):
        rebind(settings.state_root, inside, settings.vault_root)


def test_vault_nested_in_larger_git_repo_needs_ignore_rule(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    vault = repo / "sub"
    vault.mkdir()
    with pytest.raises(VaultNotIgnored):
        check_vault_ignored(vault, "Weaver")
    (repo / ".gitignore").write_text("sub/Weaver/\n")
    check_vault_ignored(vault, "Weaver")


def test_missing_vault_directory_passes(tmp_path):
    check_vault_ignored(tmp_path / "absent", "Weaver")


def test_gitignore_fallback_refuses_unevaluable_parent_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _git_init(repo)
    vault = repo / "sub"
    vault.mkdir()

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(VaultNotIgnored):
        check_vault_ignored(vault, "Weaver")
