"""Read-only, no-model measurement of bounded Wiki planning cycles."""

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from weaver.context import DictionarySnapshot
from weaver.schema import Thread
from weaver.wiki.document import MEMO, sections
from weaver.wiki.selection import (
    MAX_NEW_THREADS,
    MAX_RELATED_THREADS,
    EvidenceIndex,
    affected,
)
from weaver.wiki.store import WikiStore


def measure(
    store: WikiStore,
    threads: list[Thread],
    dictionary: DictionarySnapshot,
    *,
    after_seq: int = 0,
    sources: dict[str, str] | None = None,
) -> dict[str, object]:
    """Do not scan, bootstrap, write metadata or advance any real bookmark.

    This models page compilation inputs; digest full-period payloads are reported
    separately by the compiler and are deliberately not counted as partial pages.
    """
    pages = {}
    for file in sorted((store.root / "pages").rglob("*.md")):
        path = file.relative_to(store.root).as_posix()
        _, body, _ = store.read(path)
        pages[path] = body
    sources = sources or {}
    cycles: list[dict[str, object]] = []
    pending = sorted((t for t in threads if t.seq > after_seq), key=lambda t: t.seq)
    with tempfile.TemporaryDirectory(prefix="weaver-wiki-measure-") as directory:
        index = EvidenceIndex(Path(directory) / "index.db", threads, dictionary)
        for offset in range(0, len(pending), MAX_NEW_THREADS):
            new = pending[offset : offset + MAX_NEW_THREADS]
            selected = {}
            ids = {t.thread_id for t in new}
            modes = {}
            for path, body in pages.items():
                meta = store.metadata(path)
                keys = affected(meta, new, index) & {
                    part.key for part in sections(body)
                }
                if not keys:
                    continue
                domain = {part.key for part in sections(body)} - {
                    MEMO,
                    "## 근거",
                    "__intro__",
                }
                if len(keys - {"__intro__"}) > len(domain) / 2:
                    keys = domain | {"__intro__"}
                    modes[path] = "full: selected sections exceed half"
                mapping = meta.get("sections", {})
                if isinstance(mapping, dict):
                    for key in keys:
                        data = mapping.get(key)
                        if isinstance(data, dict) and isinstance(
                            data.get("thread_ids"), list
                        ):
                            mapped = data["thread_ids"]
                            assert isinstance(mapped, list)
                            if len(mapped) <= MAX_RELATED_THREADS:
                                ids.update(str(value) for value in mapped)
                            else:
                                modes[path] = "merge-only: section evidence cap"
                selected[path] = sorted(keys)
            related = [t for t in threads if t.thread_id in ids]
            old = {
                "threads": [t.model_dump(mode="json") for t in threads],
                "existing_pages": pages,
                "sources": sources,
            }
            new_payload = {
                "threads": [t.model_dump(mode="json") for t in related],
                "existing_pages": {
                    path: "".join(
                        part.text for part in sections(pages[path]) if part.key in keys
                    )
                    for path, keys in selected.items()
                },
                "sources": {
                    source: sources[source][
                        : min(
                            12000, 32000 // max(1, len({t.source_id for t in related}))
                        )
                    ]
                    for source in {t.source_id for t in related}
                    if source in sources
                },
            }

            def size(payload: object) -> dict[str, int]:
                count = len(json.dumps(payload, ensure_ascii=False).encode())
                return {"prompt_bytes": count, "estimated_tokens": (count + 3) // 4}

            old_size, new_size = size(old), size(new_payload)
            if new_size["estimated_tokens"] > 60000:
                modes.update(
                    {
                        path: "full: estimated input exceeds 60000 tokens"
                        for path in selected
                    }
                )
            cycles.append(
                {
                    "after_seq": after_seq if not cycles else pending[offset - 1].seq,
                    "through_seq": max(t.seq for t in new),
                    "selected_sections": selected,
                    "modes": modes,
                    "old_full": old_size,
                    "new_partial": new_size,
                    "unclassified_threads": [t.thread_id for t in new]
                    if not selected
                    else [],
                }
            )
    return {
        "dry_run": True,
        "llm_calls": 0,
        "estimator": "UTF-8 bytes / 4, page payload only; not provider billing",
        "cycles": cycles,
    }


def bookmark(path: Path) -> int:
    if not path.exists():
        return 0
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return int(db.execute("SELECT seq FROM bookmark").fetchone()[0])
