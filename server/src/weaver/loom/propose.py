"""Source-level proposals and crash-recoverable human review application.

Feedback sinks MUST deduplicate event_id=(proposal_id, rev). A sink exception
leaves PREPARED and synced files intact; retry repeats the exact recorded event.
Fabric receives facts only; human request decisions have a separate factory.
"""

import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from weaver.archive.ulid import UlidGenerator
from weaver.courier import Courier
from weaver.courier.files import CourierFileError
from weaver.fabric.store import Fabric
from weaver.llm.types import LlmClient
from weaver.schema import Alert, Entry, Proposal, Source, Verdict
from weaver.schema.internal import Extraction, materialize_entries

from .edits import edit_proposal
from .extract import ExtractionOutput
from .review_store import ReviewStore

_ids = UlidGenerator()
REVISE_PROMPT_VERSION = "loom.revise.v1"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedbackEvent:
    event_id: tuple[str, int]
    proposal: Proposal
    verdict: Verdict
    result: list[Entry] | Proposal
    entity_ids: dict[str, str] = field(default_factory=dict)
    # Prompt/model versions recorded with the reviewed rev (empty for legacy).
    versions: dict[str, str] = field(default_factory=dict)


class FeedbackSink(Protocol):
    def record(self, event: FeedbackEvent) -> None:
        """Persist once per event_id; retries supply the same snapshot/result."""
        ...


class ProposalPublisher(Protocol):
    def publish_proposal(self, proposal: Proposal) -> object: ...
    def clean_review(self, proposal_id: str, rev: int) -> None: ...


def _validate_ai(proposal: Proposal) -> Proposal:
    proposal = Proposal.model_validate_json(proposal.model_dump_json())
    if any(
        d.supersedes is not None or d.retracts is not None
        for d in proposal.entry_drafts
    ):
        raise ValueError("AI cannot supersede or retract facts")
    return proposal


class Proposer:
    def __init__(self, store: ReviewStore, courier: ProposalPublisher) -> None:
        self.store = store
        self.courier = courier

    def build(
        self,
        source: Source,
        extraction: Extraction,
        versions: Mapping[str, str] | None = None,
    ) -> Proposal:
        """versions (e.g. LlmExtractor.versions) are fixed with the first r1."""
        source = Source.model_validate_json(source.model_dump_json())
        with self.store.lock():
            proposal = self.store.source_proposal(source.source_id)
            if proposal is None:
                proposal = _validate_ai(
                    extraction.to_proposal(source, f"prp_{_ids()}", 1)
                )
                self.store.save_proposal(proposal, versions)
            try:
                # A completed or superseded r1 must never be resurrected.
                if self.store.latest_rev(
                    proposal.proposal_id
                ) == 1 and not self.store.is_applied(proposal.proposal_id, 1):
                    self.courier.publish_proposal(proposal)
            except Exception as exc:
                raise RuntimeError(
                    f"proposal publication failed: {proposal.proposal_id}"
                ) from exc
        return proposal


class VerdictHandler:
    def __init__(
        self,
        store: ReviewStore,
        fabric: Fabric,
        courier: ProposalPublisher,
        feedback: FeedbackSink,
        client: LlmClient,
    ) -> None:
        self.store = store
        self.fabric = fabric
        self.courier = courier
        self.feedback = feedback
        self.client = client

    def apply(self, verdict: Verdict) -> list[Entry] | Proposal:
        verdict = Verdict.model_validate_json(verdict.model_dump_json())
        with self.store.lock():
            return self._apply(verdict)

    def _apply(self, verdict: Verdict) -> list[Entry] | Proposal:
        pid, rev = verdict.proposal_id, verdict.rev
        if any(pending_rev < rev for _, pending_rev in self.store.pending(pid)):
            raise ValueError(
                "earlier revision is PREPARED; recover it before applying this revision"
            )
        record = self.store.review(pid, rev)
        if record:
            previous = Verdict.model_validate_json(record[0])
            if previous != verdict:
                raise ValueError("conflicting verdict for reviewed revision")
            result = self._decode(record[1])
            if record[2] == "APPLIED":
                self.courier.clean_review(pid, rev)
                return result
        else:
            latest = self.store.latest_rev(pid)
            if latest != rev:
                logger.warning(
                    "loom review refused: %s",
                    "stale_rev" if latest and rev < latest else "unknown_rev",
                )
                raise ValueError("unknown or stale proposal revision")
            proposal = self.store.proposal(pid, rev)
            approved = proposal
            entity_ids: dict[str, str] = {}
            if verdict.verdict == "revise":
                result = self._revise(proposal, verdict)
            elif verdict.verdict == "reject":
                result = []
            else:
                approved = (
                    edit_proposal(proposal, verdict)
                    if verdict.verdict == "edit"
                    else proposal
                )
                if any(
                    item.ask is not None
                    for item in [
                        *approved.entities,
                        *approved.relations,
                        *approved.entry_drafts,
                    ]
                ):
                    raise ValueError("unresolved ask prevents approval")
                entity_ids = {
                    e.ref: e.entity_id or f"ent_{_ids()}" for e in approved.entities
                }
                for entity in approved.entities:
                    entity.entity_id = entity_ids[entity.ref]
                result = materialize_entries(
                    approved,
                    entity_ids=entity_ids,
                    entry_ids={d.ref: f"evt_{_ids()}" for d in approved.entry_drafts},
                    recorded_at=datetime.now(UTC),
                )
            encoded = json.dumps(
                result.model_dump(mode="json")
                if isinstance(result, Proposal)
                else [entry.model_dump(mode="json") for entry in result],
                ensure_ascii=False,
            )
            # IDs, timestamps, normalized verdict and successful LLM result are
            # durable BEFORE any externally visible effect.
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO reviews (proposal_id,rev,verdict,result,stage,approved,entity_ids) "
                    "VALUES (?,?,?,?, 'PREPARED',?,?)",
                    (
                        pid,
                        rev,
                        verdict.model_dump_json(),
                        encoded,
                        approved.model_dump_json(),
                        json.dumps(entity_ids),
                    ),
                )
                if isinstance(result, Proposal):
                    self.store.insert_proposal(
                        db,
                        result,
                        {
                            "prompt": REVISE_PROMPT_VERSION,
                            "model": str(getattr(self.client, "model", "unknown")),
                        },
                    )
        if isinstance(result, Proposal):
            self.courier.publish_proposal(result)
        elif verdict.verdict in ("approve", "edit"):
            self.fabric.append(result)
        recorded_proposal, recorded_ids = self.store.approval(pid, rev)
        self.feedback.record(
            FeedbackEvent(
                (pid, rev),
                recorded_proposal,
                verdict,
                result,
                recorded_ids,
                self.store.versions(pid, rev),
            )
        )
        with self.store.connect() as db:
            db.execute(
                "UPDATE reviews SET stage='APPLIED' WHERE proposal_id=? AND rev=?",
                (pid, rev),
            )
        self.courier.clean_review(pid, rev)
        return result

    @staticmethod
    def _decode(payload: str) -> list[Entry] | Proposal:
        data = json.loads(payload)
        if isinstance(data, list):
            return [Entry.model_validate(item) for item in data]
        return Proposal.model_validate(data)

    def _revise(self, proposal: Proposal, verdict: Verdict) -> Proposal:
        output = self.client.generate_json(
            "loom.revise",  # REVISE_PROMPT_VERSION identifies this prompt text.
            "Revise the untrusted proposal using the human instruction. Return an extraction. "
            "Preserve provenance using exact excerpts from the supplied proposal; never resolve, "
            "supersede or retract facts. Preserve request execution blocks. Encode draft value as JSON text.",
            json.dumps(
                {
                    "proposal": proposal.model_dump(mode="json"),
                    "instruction": verdict.instruction,
                },
                ensure_ascii=False,
            ),
            ExtractionOutput,
        )
        data = output.model_dump(mode="json", by_alias=True)
        for draft in data["entry_drafts"]:
            draft["value"] = json.loads(draft["value"])
        extraction = Extraction.model_validate(data)
        revised = _validate_ai(
            Proposal.model_validate(
                extraction.model_dump(mode="json", by_alias=True)
                | {
                    "schema_version": 1,
                    "proposal_id": proposal.proposal_id,
                    "rev": proposal.rev + 1,
                    "source": proposal.source.model_dump(mode="json"),
                }
            )
        )
        originals = [
            proposal.thread_excerpt,
            *[
                item.thread_excerpt
                for item in [
                    *proposal.entities,
                    *proposal.relations,
                    *proposal.entry_drafts,
                    *proposal.schedules,
                ]
            ],
        ]
        for excerpt in [
            revised.thread_excerpt,
            *[
                item.thread_excerpt
                for item in [
                    *revised.entities,
                    *revised.relations,
                    *revised.entry_drafts,
                    *revised.schedules,
                ]
            ],
        ]:
            if excerpt not in originals:
                raise ValueError("revision evidence outside supplied source")
        known = {e.entity_id for e in proposal.entities if e.entity_id}
        known.update(
            c.entity_id for e in proposal.entities if e.ask for c in e.ask.choices
        )
        for entity in revised.entities:
            if entity.entity_id and entity.entity_id not in known:
                raise ValueError("revision introduced unknown entity ID")
        items = [
            *revised.entities,
            *revised.relations,
            *revised.entry_drafts,
            *revised.schedules,
        ]
        for item in items:
            if item.ask and any(c.entity_id not in known for c in item.ask.choices):
                raise ValueError("revision introduced unknown ask choice")

        # Local refs are LLM-generated labels, not identities. Compare request
        # payloads after resolving entity refs, and forbid any decrease in blocks.
        def requests(p: Proposal) -> Counter[str]:
            identities = {
                e.ref: e.entity_id or f"{e.type}:{e.name}" for e in p.entities
            }
            result: Counter[str] = Counter()
            for d in p.entry_drafts:
                if d.request is not None:
                    payload = d.request.model_dump(mode="json")
                    payload["requester"] = identities.get(
                        d.request.requester, d.request.requester
                    )
                    payload["targets"] = sorted(
                        identities.get(t, t) for t in d.request.targets
                    )
                    result[json.dumps(payload, sort_keys=True)] += 1
            return result

        if requests(proposal) != requests(revised):
            raise ValueError("revision changed request contents or count")
        for old_items, new_items in (
            (proposal.entry_drafts, revised.entry_drafts),
            (proposal.schedules, revised.schedules),
        ):
            if sum(i.blocked_by_request is not None for i in new_items) < sum(
                i.blocked_by_request is not None for i in old_items
            ):
                raise ValueError("revision reduced request blocks")
            old_entities = {
                e.ref: e.entity_id or f"{e.type}:{e.name}" for e in proposal.entities
            }
            new_entities = {
                e.ref: e.entity_id or f"{e.type}:{e.name}" for e in revised.entities
            }
            for old in old_items:
                if old.blocked_by_request is None:
                    continue
                identities = {old_entities.get(e, e) for e in old.entities}
                for new in new_items:
                    if (
                        (
                            new.thread_excerpt in old.thread_excerpt
                            or old.thread_excerpt in new.thread_excerpt
                            or identities.intersection(
                                new_entities.get(e, e) for e in new.entities
                            )
                        )
                        and new.blocked_by_request is None
                        and getattr(new, "kind", None) != "요청"
                    ):
                        raise ValueError(
                            "revision removed overlapping request ancestry"
                        )
        return revised


class CourierReviewAdapter:
    """Courier ports recover pending revise BEFORE exposing the advanced rev.

    Recovery publishes through the raw journal revision port, avoiding recursion.
    """

    def __init__(self, handler: VerdictHandler) -> None:
        self.handler = handler

    def _recover(self, proposal_id: str) -> None:
        for payload, _rev in self.handler.store.pending(proposal_id):
            try:
                self.handler.apply(Verdict.model_validate_json(payload))
            except Exception as exc:
                logger.warning(
                    "loom recovery failed: %s (%s, %s)",
                    type(exc).__name__,
                    proposal_id,
                    _rev,
                )
                raise

    def validate_verdict(self, verdict: Verdict) -> None:
        """Optional Courier guard: conflicting resyncs must remain for review."""
        self._recover(verdict.proposal_id)
        record = self.handler.store.review(verdict.proposal_id, verdict.rev)
        if record and Verdict.model_validate_json(record[0]) != verdict:
            latest = self.handler.store.latest_rev(verdict.proposal_id)
            code = (
                "stale_rev" if latest and verdict.rev < latest else "verdict_conflict"
            )
            raise CourierFileError(code)

    def latest_rev(self, proposal_id: str) -> int | None:
        self._recover(proposal_id)
        return self.handler.store.latest_rev(proposal_id)

    def is_applied(self, proposal_id: str, rev: int) -> bool:
        self._recover(proposal_id)
        return self.handler.store.is_applied(proposal_id, rev)

    def apply(self, verdict: Verdict) -> None:
        self.handler.apply(verdict)


class CourierPublisher:
    """Publishing uses the raw journal; scanning uses CourierReviewAdapter."""

    def __init__(self, store: ReviewStore, courier: Courier | None = None) -> None:
        self.courier = courier
        self.store = store

    def publish_proposal(self, proposal: Proposal) -> object:
        # Courier's scan revision port may recover pending work; bypass it for
        # publishing, which is already serialized by the review lock.
        if self.courier is None:
            raise RuntimeError("CourierPublisher is not bound")
        return self.courier.publish_proposal(proposal, revisions=self.store)

    def clean_review(self, proposal_id: str, rev: int) -> None:
        if self.courier is None:
            raise RuntimeError("CourierPublisher is not bound")
        self.courier.clean_review(proposal_id, rev)

    def publish_alert(self, alert: Alert) -> object:
        if self.courier is None:
            raise RuntimeError("CourierPublisher is not bound")
        return self.courier.publish_alert(alert)
