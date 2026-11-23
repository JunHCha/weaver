"""Sync configured reference vaults into the index."""

import shutil
from collections.abc import Sequence
from dataclasses import replace

from weaver.config import ReferenceVault, ServerConfig, reference_clone_dir
from weaver.context import ContextDictionary
from weaver.context.tree import expand, home_path
from weaver.reference.git import (
    ReferenceSyncError,
    changes,
    sync_clone,
)
from weaver.reference.index import (
    DB_NAME,
    Mapper,
    ReferenceIndex,
    SyncReport,
    fts5_trigram_available,
    walk,
)
from weaver.wiki.store import normalize_path


def _git_mapper(sphere: str) -> Mapper:
    def mapper(tags: Sequence[str], folder: str) -> tuple[str, ...]:
        if tags:
            return tuple(dict.fromkeys(f"{sphere}/{tag}" for tag in tags))
        return (f"{sphere}/{folder}" if folder else sphere,)

    return mapper


def _relative_folder(folder: str, include: Sequence[str]) -> str:
    """Drop an include prefix's own segments below its PARA head.

    ``include=["2. Area/가족"]`` with folder ``2. Area/가족/행사`` becomes
    ``2. Area/행사``, so the included folder is not repeated in the tree path.
    """
    parts = normalize_path(folder).split("/") if folder else []
    best: list[str] = []
    for prefix in include:
        want = normalize_path(prefix).strip("/").split("/")
        if parts[: len(want)] == want and len(want) > len(best):
            best = want
    if len(best) <= 1:
        return folder
    return "/".join([parts[0], *parts[len(best) :]])


def _home_mapper(sphere: str, include: Sequence[str] = ()) -> Mapper:
    def mapper(tags: Sequence[str], folder: str) -> tuple[str, ...]:
        # Personal vaults do not use tags; the folder always decides. Archive
        # chunks stay searchable but carry no tree tags.
        path = home_path(sphere, _relative_folder(folder, include))
        return () if path is None else (path,)

    return mapper


def _sync_git(
    config: ServerConfig,
    vault: ReferenceVault,
    index: ReferenceIndex,
    *,
    full: bool,
    reclone: bool,
) -> SyncReport:
    assert vault.git_remote is not None
    clone = reference_clone_dir(config.state_root, vault.sphere)
    if reclone and clone.exists():
        shutil.rmtree(clone)
    head = sync_clone(vault.git_remote, vault.branch, clone)
    last = None if full or reclone else index.last_commit(vault.sphere)
    diff = changes(clone, last, head)
    mapper = _git_mapper(vault.sphere)
    wiki = config.wiki.folder
    if diff is None:
        return index.index_files(
            vault.sphere, clone, None, commit=head, wiki_folder=wiki, mapper=mapper
        )
    removed = index.remove(vault.sphere, [c.path for c in diff if c.status == "D"])
    report = index.index_files(
        vault.sphere,
        clone,
        [c.path for c in diff if c.status != "D"],
        commit=head,
        wiki_folder=wiki,
        mapper=mapper,
    )
    return replace(report, removed=removed)


def _sync_home(
    config: ServerConfig, vault: ReferenceVault, index: ReferenceIndex, *, full: bool
) -> SyncReport:
    wiki = config.wiki.folder
    mapper = _home_mapper(vault.sphere, vault.include)
    if full:
        return index.index_files(
            vault.sphere,
            config.vault_root,
            None,
            commit=None,
            wiki_folder=wiki,
            mapper=mapper,
            include=vault.include,
        )
    present = walk(config.vault_root, wiki, vault.include)
    gone = index.known_paths(vault.sphere) - {p for p in present}
    removed = index.remove(vault.sphere, gone)
    report = index.index_files(
        vault.sphere,
        config.vault_root,
        present,
        commit=None,
        wiki_folder=wiki,
        mapper=mapper,
    )
    return replace(report, removed=removed)


def sync_all(
    config: ServerConfig,
    *,
    sphere: str | None = None,
    full: bool = False,
    reclone: bool = False,
) -> list[SyncReport]:
    vaults = [v for v in config.reference.vaults if sphere in (None, v.sphere)]
    if sphere is not None and not vaults:
        raise ReferenceSyncError("unknown reference sphere")
    if not fts5_trigram_available():
        raise ReferenceSyncError(
            "SQLite FTS5 trigram tokenizer is unavailable; "
            "reference sync needs SQLite 3.34+ with FTS5"
        )
    index = ReferenceIndex(config.state_root / DB_NAME)
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    reports: list[SyncReport] = []
    for vault in vaults:
        if vault.source == "git":
            report = _sync_git(config, vault, index, full=full, reclone=reclone)
        else:
            report = _sync_home(config, vault, index, full=full)
        dictionary.sync_tree(
            vault.sphere, expand(vault.sphere, index.tree_tags(vault.sphere))
        )
        reports.append(report)
    return reports
