"""Local append-only timeline persistence (state folding is a separate concern)."""

from .store import (
    EntryConflict,
    EntryQuery,
    Fabric,
    FabricError,
    FabricStore,
    InvalidReference,
    SchemaVersionError,
)

__all__ = [
    "EntryConflict",
    "EntryQuery",
    "Fabric",
    "FabricError",
    "FabricStore",
    "InvalidReference",
    "SchemaVersionError",
]
