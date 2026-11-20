"""Durable domain journal ports shared by file and HTTP adapters."""

from typing import Protocol

from weaver.schema import Verdict


class PackageRevisions(Protocol):
    def latest_rev(self, package_id: str) -> int | None:
        """Return the latest durably recorded package revision, or None if unknown."""
        ...


class DecisionStore(Protocol):
    def is_applied(self, package_id: str, rev: int) -> bool:
        """Return whether the verdict for this revision was durably applied."""
        ...

    def apply(self, verdict: Verdict) -> None:
        """Apply one validated verdict; raise on failure."""
        ...
