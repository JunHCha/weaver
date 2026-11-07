"""Injected boundaries that Courier queries but does not implement.

The durable stores behind these protocols live elsewhere: SourceStore
persistence and receipts publication in ``weaver.archive`` (#18), Loom revision
state and VerdictHandler persistence in #26. Courier only moves and validates
files; every "already done?" answer comes from these ports so a file that
reappears after restart or resync is not reapplied.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

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


type CaptureStatus = Literal["new", "stored", "conflict"]


class CaptureConflict(Exception):
    """This capture_id is already stored with a different sha256.

    The stored original and its receipt are never replaced; the files stay in
    outbox for a human.
    """


class CaptureStore(Protocol):
    def status(self, capture_id: str, sha256: str) -> CaptureStatus:
        """Classify by capture_id, never by body hash alone.

        ``stored``: this capture_id is durably stored with this sha256.
        ``conflict``: this capture_id is stored with a different sha256.
        ``new``: unknown capture_id, even if the same body is already stored
        under another capture; ``accept`` then links it to that Source.
        """
        ...

    def confirm(self, capture_id: str) -> None:
        """Re-publish the receipt of a stored capture if it is missing.

        Courier calls this only while the manifest is still in outbox, so a
        receipt the phone already cleaned up is not resurrected afterwards.
        """
        ...

    def accept(self, capture: VerifiedCapture) -> None:
        """Hand a verified capture onward (persist, then publish its receipt).

        Raise ``CaptureConflict`` for a capture_id stored with another sha256.
        Any other exception leaves the files in place for the next scan.
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
