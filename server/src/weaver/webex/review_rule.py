"""The single replaceable point for Webex correction review burden.

Webex Space-day corrections are independent Sources, each normally with its own
one-page Loom review (`ReviewEveryCorrection`, the document baseline). The
default, decided by the user on 2026-11-22 (option A), is
`SupersedeUnreviewedCorrections`: when corrections of one Space-day pile up, only
the newest undecided one stays reviewable and the older undecided ones become
"superseded" (a persisted review state, never a deletion). Any other rule is a
drop-in `CorrectionReviewRule` passed to `SummaryProcessor(correction_rule=...)`.
No rule may rewrite or replace an approved Thread; originals (revision 0) are
never offered to a rule and never superseded.
"""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class CorrectionCase:
    """A parsed correction Source (revision > 0); IDs stay private provenance."""

    source_id: str
    space_id: str
    day: str | None
    part: int
    revision: int
    corrects_capture_id: str | None


@dataclass(frozen=True)
class ReviewDecision:
    """What to do with one correction Source's Loom review.

    `review` creates the page now and, afterwards, supersedes every Source in
    `supersedes` whose review is still undecided. `supersede` makes this Source's
    own review superseded by `superseded_by` (no page, final, nothing deleted).
    `skip` creates no page and leaves the Source re-evaluable: the rule is asked
    again on later passes, after the LLM policy check.
    """

    action: Literal["review", "skip", "supersede"] = "review"
    supersedes: tuple[str, ...] = ()
    superseded_by: str | None = None


REVIEW = ReviewDecision()


@dataclass(frozen=True)
class CorrectionStatus:
    """One known correction Source of a Space-day, as the review ledger sees it."""

    source_id: str
    revision: int
    part: int
    message_ids: frozenset[str]
    reviewed: bool  # a Verdict was received (approved or in flight)
    superseded_by: str | None = None


class CorrectionLedger(Protocol):
    def corrections(self, space_id: str, day: str) -> list[CorrectionStatus]:
        """Correction Sources (revision > 0) of this Space-day, parsed so far."""
        ...


class CorrectionReviewRule(Protocol):
    def decide(self, case: CorrectionCase) -> ReviewDecision:
        """The review decision for this correction Source."""
        ...


class ReviewEveryCorrection:
    """Document baseline: one review per correction Source, no relief."""

    def decide(self, case: CorrectionCase) -> ReviewDecision:
        return REVIEW


class SupersedeUnreviewedCorrections:
    """Option A: only the newest undecided correction revision stays reviewable.

    Parts of the newest revision all stay (each is its own page). An older
    correction is superseded only while no Verdict was received for it, and only
    by a newer correction. With `require_coverage` (default) it must also be
    redundant: every message it carries is carried again by the newer
    corrections, because a correction holds only the messages that changed and
    superseding a different message would drop it from review altogether.
    """

    def __init__(self, ledger: CorrectionLedger, *, require_coverage: bool = True):
        self.ledger = ledger
        self.require_coverage = require_coverage

    def _replacement(
        self, item: CorrectionStatus, items: list[CorrectionStatus]
    ) -> CorrectionStatus | None:
        newer = [
            other
            for other in items
            if other.revision > item.revision and other.superseded_by is None
        ]
        if not newer:
            return None
        covered = frozenset().union(*(other.message_ids for other in newer))
        if self.require_coverage and not item.message_ids <= covered:
            return None
        return max(newer, key=lambda other: (other.revision, other.part))

    def decide(self, case: CorrectionCase) -> ReviewDecision:
        if case.day is None or case.revision == 0:
            return REVIEW
        items = self.ledger.corrections(case.space_id, case.day)
        me = next((i for i in items if i.source_id == case.source_id), None)
        if me is None or me.reviewed:
            return REVIEW
        if me.superseded_by is not None:
            return ReviewDecision("supersede", superseded_by=me.superseded_by)
        replacement = self._replacement(me, items)
        if replacement is not None:
            return ReviewDecision("supersede", superseded_by=replacement.source_id)
        older = tuple(
            sorted(
                i.source_id
                for i in items
                if i.revision < me.revision
                and not i.reviewed
                and i.superseded_by is None
                and self._replacement(i, items) is not None
            )
        )
        return ReviewDecision("review", supersedes=older)
