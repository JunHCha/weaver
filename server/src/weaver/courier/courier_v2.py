"""Version 2 review files on the shared Courier transport."""

from pathlib import Path
from typing import cast

from weaver.config import CourierPaths
from weaver.schema import Proposal
from weaver.schema.v2 import ReviewPackage, Verdict

from .atomic import write_json_atomic
from .courier import Courier, RevisionError, ScanReport, _ask, logger
from .files import (
    ALERT_NAME,
    CourierFileError,
    load_versioned,
    parse_revision_name,
    revision_file_name,
)
from .ports import (
    CaptureStore,
    DecisionStore,
    DecisionStoreV2,
    PackageRevisions,
    ProposalRevisions,
)


class CourierV2(Courier):
    """Courier transport with v2 review packages and decisions."""

    def __init__(
        self,
        paths: CourierPaths,
        captures: CaptureStore,
        revisions: PackageRevisions,
        decisions: DecisionStoreV2,
    ) -> None:
        super().__init__(
            paths,
            captures,
            cast(ProposalRevisions, revisions),
            cast(DecisionStore, decisions),
        )
        self._v2_decisions = decisions

    def publish_proposal(
        self, proposal: Proposal, *, revisions: ProposalRevisions | None = None
    ) -> Path:
        """Reject legacy review publications on the v2 transport."""
        raise TypeError("Courier v2 accepts ReviewPackage only")

    def publish_package(
        self, proposal: ReviewPackage, *, revisions: PackageRevisions | None = None
    ) -> Path:
        """Write ``<proposal_id>.r<rev>.json`` atomically, then drop older revs.

        Loom must record ``proposal.revision`` durably before calling this.
        """
        latest = (revisions or self._revisions).latest_rev(proposal.package_id)
        if latest is None or proposal.revision != latest:
            raise RevisionError("proposal rev is not the recorded latest rev")
        final = self.paths.proposals / revision_file_name(
            proposal.package_id, proposal.revision
        )
        write_json_atomic(final, proposal)
        self._remove_revisions(proposal.package_id, below=proposal.revision)
        return final

    def _scan_proposals(
        self, report: ScanReport, current: dict[tuple[str, str], str]
    ) -> None:
        """Drop superseded or already-decided revs that reappear after resync."""
        self._sweep_temporaries("proposals", self.paths.proposals, report, current)
        for path in self._entries("proposals", self.paths.proposals, report, current):
            if ALERT_NAME.fullmatch(path.name):
                continue  # Alerts are consumed and removed by the phone.
            name = parse_revision_name(path.name)
            if name is None:
                continue
            try:
                proposal = load_versioned(path, ReviewPackage, version=2)
                if proposal.package_id != name.proposal_id:
                    raise CourierFileError("id_mismatch")
                if proposal.revision != name.rev:
                    raise CourierFileError("rev_mismatch")
                pid, rev = name.proposal_id, name.rev
                latest = _ask(self._revisions.latest_rev, pid)
                if latest is None:
                    raise CourierFileError("unknown_proposal")
                if rev > latest:
                    raise CourierFileError("unknown_rev")
                if rev < latest or _ask(self._v2_decisions.is_applied, pid, rev):
                    path.unlink(missing_ok=True)
                    report.pruned.append(path.name)
            except CourierFileError as error:
                self._report(current, "proposals", path.name, error.code, report)
            except OSError:
                self._report(current, "proposals", path.name, "io_error", report)

    def _scan_decisions(
        self, report: ScanReport, current: dict[tuple[str, str], str]
    ) -> None:
        for path in self._entries("decisions", self.paths.decisions, report, current):
            name = parse_revision_name(path.name)
            if name is None:
                continue
            try:
                verdict = load_versioned(path, Verdict, version=2)
                if verdict.package_id != name.proposal_id:
                    raise CourierFileError("id_mismatch")
                if verdict.revision != name.rev:
                    raise CourierFileError("rev_mismatch")
                pid, rev = name.proposal_id, name.rev
                # Loom's optional guard checks conflicting resynced decisions
                # before the legacy applied-file cleanup shortcut.
                validate = getattr(self._v2_decisions, "validate_verdict", None)
                if validate is not None:
                    try:
                        validate(verdict)
                    except CourierFileError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "courier recovery failed: %s (%s, %s)",
                            type(exc).__name__,
                            pid,
                            rev,
                        )
                        raise CourierFileError("store_failed") from None
                if _ask(self._v2_decisions.is_applied, pid, rev):
                    self._clean_decision(path, pid, rev, report)
                    continue
                latest = _ask(self._revisions.latest_rev, pid)
                if latest is None:
                    raise CourierFileError("unknown_proposal")
                if rev < latest:
                    raise CourierFileError("stale_rev")
                if rev > latest:
                    raise CourierFileError("unknown_rev")
                try:
                    self._v2_decisions.apply(verdict)
                except Exception as exc:
                    logger.warning(
                        "courier apply failed: %s (%s, %s)",
                        type(exc).__name__,
                        pid,
                        rev,
                    )
                    raise CourierFileError("apply_failed") from None
                if not _ask(self._v2_decisions.is_applied, pid, rev):
                    raise CourierFileError("apply_unconfirmed")
                report.applied.append((pid, rev))
                self._clean_decision(path, pid, rev, report)
            except CourierFileError as error:
                self._report(current, "decisions", path.name, error.code, report)
            except OSError:
                self._report(current, "decisions", path.name, "io_error", report)
