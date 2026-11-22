"""The single replaceable point for Webex correction review burden.

Webex Space-day corrections are independent Sources, so by the pilot document
each gets its own one-page Loom review (ReviewEveryCorrection, the default).
How to lighten the load when corrections pile up is an open user decision; any
chosen rule (for example "only the newest unreviewed correction of a Space-day
stays reviewable") is a drop-in `CorrectionReviewRule` passed to
`SummaryProcessor(correction_rule=...)`. Nothing else consults corrections, and
no rule may rewrite or replace an approved Thread: corrections only add reviews.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CorrectionCase:
    """A parsed correction Source (revision > 0); IDs stay private provenance."""

    source_id: str
    space_id: str
    day: str | None
    part: int
    revision: int
    corrects_capture_id: str | None


class CorrectionReviewRule(Protocol):
    def create_review(self, case: CorrectionCase) -> bool:
        """True to create this Source's one-page Loom review now."""
        ...


class ReviewEveryCorrection:
    """Document default: one review per correction Source, no relief."""

    def create_review(self, case: CorrectionCase) -> bool:
        return True
