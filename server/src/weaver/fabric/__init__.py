"""Local append-only timeline persistence and deterministic state projections."""

from .state import InvalidSetOperation
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
    "InvalidSetOperation",
    "SchemaVersionError",
]
