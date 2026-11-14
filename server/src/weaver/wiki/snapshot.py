"""Normalize view keys without rewriting the append-only Fabric log."""

import unicodedata

from weaver.schema import Thread


def canonical_threads(threads: list[Thread]) -> list[Thread]:
    return [
        t.model_copy(
            update={
                "tags": sorted({unicodedata.normalize("NFC", tag) for tag in t.tags})
            }
        )
        for t in threads
    ]
