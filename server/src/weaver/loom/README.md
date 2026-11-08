# Proposal review

`Proposer.build(source, extraction)` validates and journals a new r1 before
publishing it through Courier. Confidence, questions and excerpts are preserved.
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
and PREPARED/APPLIED stage. Fabric stores approved Entries only.

Effects proceed in order: commit PREPARED, append identical Entries or publish
the recorded revision, record feedback, commit APPLIED, clean synced files.
`FeedbackSink.record(FeedbackEvent)` **must be idempotent by event_id**, the
`(proposal_id, rev)` tuple. A sink error leaves PREPARED; retry may repeat a
successful Fabric append or publication, but uses the same durable payload.
Successful LLM output is committed before publication and reused after failure.
Interruption during the LLM call can require another call.

The scan adapter recovers PREPARED work before reporting latest revision or
applied status. Thus a revise interrupted after advancing latest rev cannot
strand its decision as stale. APPLIED cleanup can be retried by the handler or
Courier scan, including after proposal removal but before decision removal.
Conflicting verdicts, stale unseen decisions, unknown IDs/versions, invalid edits
and unresolved questions remain available for inspection. Exact duplicate
verdicts return the recorded result without repeating feedback or extraction.
If initial r1 publication fails, its journal snapshot remains available for
explicit `publisher.publish_proposal(store.proposal(proposal_id, 1))` retry.

Edits replace only whitelisted fields. Collection/item/root replacement and
append are forbidden, except whole field replacement for aliases, entities,
value, and acknowledgement with ask=null. Nested JSON replacements must target
existing keys/indices; the complete Proposal is validated afterwards. Identity
changes use only original ask choice IDs or null. Request lineage is immutable.

Revise uses `LlmClient` with the proposal's excerpts and human instruction,
without reading the original archive or vault. It verifies supplied evidence,
known entity IDs and preserved request lineage on reused refs. Excerpt matching
is a provenance check, not proof that the LLM's interpretation is correct.
