"""Injected boundaries that Courier queries but does not implement.

The durable stores behind these protocols (SourceStore persistence, receipts
publication, Loom revision state and VerdictHandler persistence) belong to #26.
Courier only moves and validates files; every "already done?" answer comes from
these ports so a file that reappears after restart or resync is not reapplied.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from weaver.schema import Manifest, Verdict


@dataclass(frozen=True)
class VerifiedCapture:
    """A manifest whose payload existed with matching size and sha256 at scan time.

    ``payload_path`` is inside outbox. The receiver must copy or re-verify it
    before trusting it, since a synced file can still be replaced afterwards.
    """

    manifest: Manifest
    manifest_path: Path
    payload_path: Path


class CaptureStore(Protocol):
    def is_captured(self, capture_id: str, sha256: str) -> bool:
        """True when this capture_id *or* payload sha256 is durably stored."""
        ...

    def accept(self, capture: VerifiedCapture) -> None:
        """Hand a verified capture onward (persist, then publish its receipt).

        Raising leaves the files in place for the next scan.
        """
        ...


class ProposalRevisions(Protocol):
    def latest_rev(self, proposal_id: str) -> int | None:
        """Durably recorded latest rev, or None for an unknown proposal."""
        ...


class DecisionStore(Protocol):
    def is_applied(self, proposal_id: str, rev: int) -> bool:
        """True only after the verdict for this rev was durably applied."""
        ...

    def apply(self, verdict: Verdict) -> None:
        """Apply one verdict; revise publishes the next rev. Raise on failure."""
        ...
