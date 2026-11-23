"""Reference vaults: company clone sync and the FTS5 reference index.

Exports load lazily so importing the CLI stays free of heavy dependencies.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from weaver.reference.git import Change, ReferenceSyncError
    from weaver.reference.index import (
        RefChunk,
        ReferenceIndex,
        SyncReport,
        find_references,
        fts5_trigram_available,
    )
    from weaver.reference.sync import sync_all

_EXPORTS = {
    "Change": "git",
    "ReferenceSyncError": "git",
    "RefChunk": "index",
    "ReferenceIndex": "index",
    "SyncReport": "index",
    "find_references": "index",
    "fts5_trigram_available": "index",
    "sync_all": "sync",
}
__all__ = [
    "Change",
    "RefChunk",
    "ReferenceIndex",
    "ReferenceSyncError",
    "SyncReport",
    "find_references",
    "fts5_trigram_available",
    "sync_all",
]


def __getattr__(name: str) -> object:
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module(f"weaver.reference.{_EXPORTS[name]}"), name)
