"""Explicit reviewed migration: immutable preview, external backup, then apply."""

import fcntl
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing
from difflib import unified_diff
from pathlib import Path

from pydantic import JsonValue

from weaver.context import DictionarySnapshot
from weaver.schema import Thread
from weaver.wiki.document import HUMAN_KEYS, hunks, sections, text_hash
from weaver.wiki.selection import section_map
from weaver.wiki.store import (
    HASH_KEY,
    HumanEditPending,
    WikiStore,
    _hash,
    _properties,
    normalize_path,
)


class WikiMigration:
    def __init__(
        self,
        store: WikiStore,
        threads: list[Thread],
        dictionary: DictionarySnapshot | None = None,
    ) -> None:
        self.store, self.threads, self.dictionary = store, threads, dictionary

    def blocker(self, path: str) -> bool:
        data = self.store._path(path).read_bytes()
        with self.store._db() as db:
            row = db.execute(
                "SELECT content, candidate FROM pages WHERE path=?", (path,)
            ).fetchone()
            if row and (row[1] is not None or row[0] != data):
                return True
            for table in ("pending", "human_edits"):
                if any(
                    json.loads(payload).get("path") == path
                    for (payload,) in db.execute(f"SELECT payload FROM {table}")
                ):
                    return True
        props = _properties(data)
        digest = props.get(HASH_KEY)
        if isinstance(digest, str):
            unsigned = b"".join(
                line
                for line in data.splitlines(keepends=True)
                if not line.startswith((HASH_KEY + ":").encode())
            )
            if _hash(unsigned) != digest:
                return True
        return False

    def human_spans(self, path: str, body: str) -> list[JsonValue]:
        result: list[JsonValue] = []
        revisions = self.store._git(
            "log", "--author=human", "--format=%H", "--", path
        ).splitlines()
        for rev in revisions:
            try:
                before = self.store._git("show", f"{rev}^:{path}")
                after = self.store._git("show", f"{rev}:{path}")
            except subprocess.CalledProcessError:
                continue
            for text in ("before", "after"):
                value = before if text == "before" else after
                value = (
                    value.split("\n---\n", 1)[-1]
                    if value.startswith("---\n")
                    else value
                )
                if text == "before":
                    before = value
                else:
                    after = value
            for h in hunks(before, after):
                if (
                    h.classification not in ("memo", "metadata")
                    and h.after.strip()
                    and h.after in body
                ):
                    span: JsonValue = {"section": h.section, "text": h.after}
                    if span not in result:
                        result.append(span)
        return result

    def preview(self) -> dict[str, JsonValue]:
        """Describe every changed file; do not write files, metadata or backups."""
        changes: list[JsonValue] = []
        skipped: list[JsonValue] = []
        errors: list[JsonValue] = []
        for file in sorted(self.store.root.rglob("*.md")):
            path = normalize_path(file.relative_to(self.store.root).as_posix())
            if not (
                path.startswith(("pages/", "Digest/"))
                or path in ("열린 요청.md", "다가오는 마감.md", "사람별 연락.md")
            ):
                continue
            try:
                props, body, base = self.store.read(path)
                if self.blocker(path):
                    skipped.append({"path": path, "reason": "먼저 컴파일러로 처리"})
                    continue
            except (ValueError, UnicodeError, OSError):
                errors.append({"path": path, "reason": "변환 불가"})
                continue
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
                mapping = section_map(
                    rendered_body,
                    selected,
                    {
                        s.key: [
                            t.thread_id
                            for t in selected
                            if t.thread_id in s.text
                            or bool(t.content and t.content in s.text)
                            or any(
                                tag in s.text
                                for tag in t.tags
                                if self.dictionary is not None
                                and (entry := self.dictionary.resolve(tag))
                                and entry.kind != "area"
                            )
                        ]
                        for s in sections(rendered_body)
                    },
                )
                for value in mapping.values():
                    if isinstance(value, dict) and not value["thread_ids"]:
                        value["status"] = "미분류"
                # Legacy pages without a resolved Fabric row retain their IDs;
                # the next real delta can still select them via state_tag.
                if not selected:
                    mapping = {
                        s.key: {
                            "thread_ids": [],
                            "status": "미분류",
                            "tags": [],
                            "source_ids": [],
                            "counterparts": [],
                            "hash": text_hash(s.text),
                            "search_text": s.text[:1000],
                        }
                        for s in sections(rendered_body)
                        if s.key not in ("## 내 메모", "## 근거")
                    }
                private = private | {
                    "sections": json.loads(json.dumps(mapping)),
                    "human_spans": self.human_spans(path, rendered_body),
                }
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
                    "moved_keys": [
                        key
                        for key in props
                        if key not in HUMAN_KEYS and key != "weaver"
                    ],
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
            "skipped": skipped,
            "errors": errors,
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
            current = self.preview()
            blocked = (
                {
                    str(item["path"])
                    for item in current["skipped"]
                    if isinstance(item, dict)
                }
                if isinstance(current["skipped"], list)
                else set()
            )
            compared = dict(plan)
            plan_changes = compared.get("changes")
            if isinstance(plan_changes, list):
                compared["changes"] = [
                    item
                    for item in plan_changes
                    if isinstance(item, dict) and str(item["path"]) not in blocked
                ]
            compared["skipped"] = current["skipped"]
            if compared != current:
                raise ValueError("migration preview is stale; preview again")
            plan = current
            self.last_report = current
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
