"""Explicit reviewed migration: immutable preview, external backup, then apply."""

import fcntl
import json
import shutil
import sqlite3
from contextlib import closing
from difflib import unified_diff
from pathlib import Path

from pydantic import JsonValue

from weaver.schema import Thread
from weaver.wiki.document import sections, text_hash
from weaver.wiki.selection import section_map
from weaver.wiki.store import HumanEditPending, WikiStore, _hash, normalize_path


class WikiMigration:
    def __init__(self, store: WikiStore, threads: list[Thread]) -> None:
        self.store, self.threads = store, threads

    def preview(self) -> dict[str, JsonValue]:
        """Describe every changed file; do not write files, metadata or backups."""
        changes: list[JsonValue] = []
        for file in sorted(self.store.root.rglob("*.md")):
            path = normalize_path(file.relative_to(self.store.root).as_posix())
            if not (
                path.startswith(("pages/", "Digest/"))
                or path in ("열린 요청.md", "다가오는 마감.md", "사람별 연락.md")
            ):
                continue
            props, body, base = self.store.read(path)
            readonly = not path.startswith("pages/")
            rendered, private = self.store.render(path, props, body, readonly=readonly)
            meta = self.store.metadata(path)
            if (
                path.startswith("pages/")
                and meta.get("sections")
                and not private
                and rendered == file.read_bytes()
            ):
                continue
            if path.startswith("pages/"):
                ids = private.get("thread_ids", meta.get("thread_ids", []))
                selected = [
                    t
                    for t in self.threads
                    if isinstance(ids, list) and t.thread_id in ids
                ]
                rendered_text = rendered.decode()
                rendered_body = (
                    rendered_text.split("\n---\n", 1)[1]
                    if rendered_text.startswith("---\n")
                    else rendered_text
                )
                mapping = section_map(rendered_body, selected)
                # Legacy pages without a resolved Fabric row retain their IDs;
                # the next real delta can still select them via state_tag.
                if not selected:
                    mapping = {
                        s.key: {
                            "thread_ids": ids if isinstance(ids, list) else [],
                            "tags": [private.get("state_tag", meta.get("state_tag"))]
                            if private.get("state_tag", meta.get("state_tag"))
                            else [],
                            "source_ids": [],
                            "counterparts": [],
                            "hash": text_hash(s.text),
                            "search_text": s.text[:1000],
                        }
                        for s in sections(rendered_body)
                        if s.key not in ("## 내 메모", "## 근거")
                    }
                private = private | {"sections": json.loads(json.dumps(mapping))}
            expected_meta = meta | private
            if rendered == file.read_bytes() and (
                not path.startswith("pages/")
                or meta
                and all(meta.get(k) == v for k, v in private.items())
            ):
                continue
            changes.append(
                {
                    "path": path,
                    "before": base,
                    "after": _hash(rendered),
                    "metadata": expected_meta,
                    "readonly": readonly,
                    "diff": "".join(
                        unified_diff(
                            file.read_text().splitlines(keepends=True),
                            rendered.decode().splitlines(keepends=True),
                            fromfile=path,
                            tofile=path,
                        )
                    ),
                }
            )
        return {
            "version": 1,
            "root": str(self.store.root.resolve()),
            "changes": changes,
        }

    def apply(
        self, plan: dict[str, JsonValue], backup: Path, *, confirmed: bool
    ) -> int:
        if not confirmed:
            raise ValueError(
                "review the preview and explicitly confirm before applying"
            )
        # Use the same lock as the resident compiler, not a separate migration lock.
        with (self.store.db_path.parent / "wiki-compiler.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if plan != self.preview():
                raise ValueError("migration preview is stale; preview again")
            root = backup.resolve()
            if root.is_relative_to(self.store.vault.resolve()) or root.is_relative_to(
                self.store.db_path.parent.resolve()
            ):
                raise ValueError("backup must be outside the vault and server state")
            if backup.exists():
                raise ValueError("backup destination must not exist")
            changes = plan["changes"]
            assert isinstance(changes, list)
            if not changes:
                return 0
            # Preflight every page before starting either backup or publication.
            for item in changes:
                assert isinstance(item, dict)
                self.store.check_write(str(item["path"]), str(item["before"]))
            backup.mkdir(parents=True)
            shutil.copytree(self.store.git_dir, backup / "wiki.git", symlinks=True)
            with (
                closing(sqlite3.connect(self.store.db_path)) as source,
                closing(sqlite3.connect(backup / "wiki.sqlite3")) as dest,
            ):
                source.backup(dest)
            (backup / "plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2)
            )
            for item in changes:
                assert isinstance(item, dict)
                path = str(item["path"])
                original = self.store._path(path)
                copy = backup / "files" / path
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, copy)
                if _hash(copy.read_bytes()) != item["before"]:
                    raise HumanEditPending(path)
            for item in changes:
                assert isinstance(item, dict)
                path = str(item["path"])
                props, body, base = self.store.read(path)
                if base != item["before"]:
                    raise HumanEditPending(path)
                written = self.store.write(
                    path,
                    props,
                    body,
                    base,
                    readonly=bool(item["readonly"]),
                    metadata="migrate wiki document",
                )
                if written != item["after"]:
                    raise ValueError("migration output differs from preview")
                metadata = item["metadata"]
                if path.startswith("pages/") and isinstance(metadata, dict):
                    self.store.write_metadata(
                        path,
                        {
                            key: value
                            for key, value in metadata.items()
                            if key not in ("hash", "id", "path")
                        },
                    )
            return len(changes)
