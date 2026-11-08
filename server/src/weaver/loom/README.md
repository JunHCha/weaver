# Proposal review

`Proposer.build(source, extraction)` validates and journals r1 before
publishing it through Courier. Under the process lock, it looks up the first r1
by persisted source_id and reuses that snapshot on every retry (even if the
caller supplies a different extraction). Reviewed/superseded r1 is returned
without republishing; a source requires a new source_id to start a new review. Confidence, questions and excerpts are preserved.
`VerdictHandler.apply(verdict)` returns Entries for approve/edit, the next
Proposal for revise, and an empty list for reject. Approval records facts only;
request-derived entries retain their request block and never resolve requests.

Wire the scan ports through `CourierReviewAdapter`, and publication through
`CourierPublisher` (its raw journal revision check avoids recursive recovery):

```python
store = ReviewStore.from_config(settings)
publisher = CourierPublisher(store)
handler = VerdictHandler(store, fabric, publisher, feedback_sink, llm_client)
reviews = CourierReviewAdapter(handler)
courier = Courier(settings.courier, capture_store, reviews, reviews)
publisher.courier = courier
proposer = Proposer(store, publisher)
```

`settings.courier` is the configured `CourierPaths`. The application runtime
owns wiring; this module does not start a watcher or call external services
on import.

The private `state_root/loom.sqlite3` journal is mode 0600, outside sync/vault.
An advisory process lock serializes reviews. SQLite commits use FULL/fullfsync.
Proposal snapshots determine the latest revision. Each review records the
validated Verdict, fixed Entry IDs/timestamps or complete revised Proposal,
and PREPARED/APPLIED stage. Approval also fixes the edited Proposal with resolved
entity_id fields and the complete `{ref: entity_id}` mapping, including entities
unused by any draft. FeedbackEvent.proposal carries this approved snapshot and
FeedbackEvent.entity_ids carries the same durable mapping on every retry.
Original proposals snapshots remain immutable. Journal v2 migrates v1 source IDs;
legacy review rows retain their original snapshot and an empty ID mapping since
unreferenced IDs were never recorded. Fabric stores approved Entries only.
Entity/Relation persistence and updating the approved EntityIndex are outside
#26: the runtime/future persistence integration must consume the approved
snapshot and mapping, rather than generating new IDs.

Effects proceed in order: commit PREPARED, append identical Entries or publish
the recorded revision, record feedback, commit APPLIED, clean synced files.
`FeedbackSink.record(FeedbackEvent)` **must be idempotent by event_id**, the
`(proposal_id, rev)` tuple. A sink error leaves PREPARED; retry may repeat a
successful Fabric append or publication, but uses the same durable payload.
Successful LLM output is committed before publication and reused after failure.
Interruption during the LLM call can require another call. The advisory flock
remains held during that call and all external effects; one slow LLM or sink
blocks every build/review using this journal. The handler has no separate lock
wait timeout or cancellation. Configure a bounded timeout/retry budget on the
injected LlmClient/transport; availability is limited by its worst-case duration.

The scan adapter recovers PREPARED work before reporting latest revision or
applied status. Thus a revise interrupted after advancing latest rev cannot
strand its decision as stale. APPLIED cleanup can be retried by the handler or
Courier scan, including after proposal removal but before decision removal.
Conflicting verdicts, stale unseen decisions, unknown IDs/versions, invalid edits
and unresolved questions remain available for inspection. Exact duplicate
verdicts return the recorded result without repeating feedback or extraction.
If initial r1 publication fails, the exception includes proposal_id. Retry the
same `build(source, extraction)` to republish its persisted r1 without creating
another proposal. Direct apply of r2 is refused while an earlier revision is
PREPARED; recover the earlier verdict through the scan adapter or handler first.

To inspect blocked recovery without exposing snapshot contents, query locally:

```sql
SELECT proposal_id, rev, stage FROM reviews WHERE stage='PREPARED' ORDER BY proposal_id, rev;
```

Run this against `state_root/loom.sqlite3` (for example with sqlite3). Recovery
warnings include only exception type and opaque proposal ID/revision, never
exception messages, excerpts, instructions or entity data. Fix the failing
Fabric/publisher/sink and rerun Courier scan or the original journaled Verdict;
do not manually advance stage or delete journal rows.

Edits replace only whitelisted fields. Collection/item/root replacement and
append are forbidden, except whole field replacement for aliases, entities,
value, and acknowledgement with ask=null. Nested JSON replacements must target
existing keys/indices; the complete Proposal is validated afterwards. Identity
changes use only original ask choice IDs or null. Request lineage is immutable.

Revise uses `LlmClient` with the proposal's excerpts and human instruction,
without reading the original archive or vault. It verifies supplied evidence,
known entity IDs and request lineage independently of generated ref labels.
Every excerpt must exactly equal one of the supplied excerpts; shortened
one-character substrings do not pass. Request payloads (including requester,
targets, content and due date, resolving local entity refs) and counts must stay
identical. A decrease in blocked draft or schedule counts rejects the entire
revision. Any revised non-request item overlapping a previously blocked item's
overlapping evidence or entity identities must remain blocked, even if another blocked
item keeps the total count unchanged. This conservative rejection policy may
reject legitimate restructuring; the user can revise again or reject the proposal.
Excerpt matching checks provenance rather than semantic truth.

Schedule ask is intentionally allowed at approval under the established policy:
only entities/relations/entry_drafts ask blocks approval. The schedule ask remains
in the approved journal snapshot and FeedbackEvent; approval does not resolve it
or authorize calendar execution.
