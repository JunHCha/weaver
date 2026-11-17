# Loom

Loom turns one Script into one reviewable event summary and, after approval,
into Fabric Threads. It never appends a fact the user did not approve.

1. `SummaryDrafts.create(source_id, script, contact_name)` asks the LLM for a
   revised Script, one summary document and at most three questions with three
   choices each (`loom.summary`). Context names (`ContextDictionary.candidates`)
   are the only hints. The `ReviewPackage` revision 1 is journaled in
   `state_root/summary.sqlite3` and then published as `<package_id>.r1.json` in
   the Courier `proposals/` folder. Extra questions are appended to the body as
   a list instead of being asked. A call Script at or below `revision_threshold`
   also gets its speech segments corrected in that same call (changed segments
   only; an invalid correction is dropped and the original is summarized). Longer
   calls are corrected beforehand by Spinner (`spinner.revise`), so they take two
   calls; long text channels take `loom.polish` plus the summary.
2. The phone sends a `Verdict` for exactly one package revision.
   `SummaryReview.apply(package, script, verdict)` handles the three actions:
   - `approve`: answers and optional feedback are applied together (`loom.approve`), the final document is
     stored as a content-addressed `EventSummary` (`archive_root/summaries/<digest prefix>/`),
     and the Threads extracted from it are appended to Fabric. Only corrections
     the reviewer chose are learned into the context dictionary.
   - `edit`: the reviewer's text is the document; only its facts are extracted
     and recorded with `actor=human`.
   - `reject`: a content-free rejection Thread is appended.
3. Schedules are `field=schedule` Threads with a `{start, end, timezone}` value.
   `schedules.approved_schedules` judges them against a complete Fabric snapshot:
   a schedule stays blocked by its request until an active decision resolves it,
   and a retracted schedule is marked retracted. The Action engine
   (`weaver.action`) turns that judgement into calendar commands. A schedule that
   the approved document describes as a change of a standing schedule carries
   `updates` (the model sees `existing_schedules`); an unfounded link is dropped.

Approval loads one dictionary snapshot and bounds `dictionary` plus
`existing_tags` to 8,000 estimated tokens, conservatively charging one token per
UTF-8 JSON byte. Existing tags get at most 2,000 of that budget, most recent
first. Dictionary hints prioritize transcript candidates, entries resolved from
recent Fabric tags, then the remaining entries. Full-snapshot validation still
accepts a known tag omitted from the prompt budget.

Tags resolve to canonical dictionary names. Unknown tags must be declared in
`new_entries` with a supported kind and optional aliases (at most 50 entries,
20 aliases per entry and 120 characters per name/alias). Unresolved tags and
conflicting aliases are dropped with content-free warnings; a valid new entry
survives a bad alias. Only new entries referenced by this approval's tags are
journaled and inserted before Fabric append. Apply-time alias collisions are
filtered inside the dictionary transaction, so retries cannot wedge the append
or overwrite later human dictionary edits.

## Durability

Every verdict is journaled with its prepared side effects (`Prepared`) before
anything is written, under an advisory lock. Replaying the same verdict after a
crash reuses the journaled summary, Thread IDs and timestamps; a different
verdict for the same revision is rejected. Thread IDs derive from the summary
digest and index, and `FabricStore.append` is idempotent by `thread_id`.

`call.py` and `kakao.py` are the Script parsers used by Spinner.

## Re-extract saved approvals

Stop the resident daemon and other SQLite writers before planning. The command
reads only `(wiki.vault_root or vault_root)/wiki.folder/pages/*.md`, matching
WikiStore assembly. Unrelated vault notes, summaries, queries, Digest files,
symlinks and page bodies never enter bootstrap. Malformed page metadata is
reported and skipped. Bootstrap has one shared 200-entry / 32 KiB UTF-8 JSON
budget (plus fixed instructions) and makes at most one model call. TODO, 확인,
확인필요 and TBD are explicitly removed even if old compiler-generated wiki
pages made them resolve as dictionary entries. Only proposed names/aliases
actually used by a planned tag are applied; unresolved independent human tags
are retained rather than silently deleted.

```sh
uv run --project server python -m weaver.app reextract --config CONFIG --plan-out /outside/state/reextract-plan.json
uv run --project server python -m weaver.app reextract --config CONFIG --apply --plan /outside/state/reextract-plan.json --yes
```

`--config`/`--llm` behave like run, `--source SOURCE_ID` limits planning, and
`--json` keeps the full report on stdout with notices on stderr. Preview shows
old/new kind, content, tags, field/value, dictionary name/alias operations,
human tag corrections and per-schedule calendar effects. It opens immutable
read-only snapshots, never creating WAL/SHM, ledgers, archives or dictionary
entries. An active, uncheckpointed SQLite journal is refused with a stop/re-plan
instruction. `--plan-out` is the sole optional preview write: a private 0600
file outside state_root containing the exact reviewed facts and operations.

LLM calls during planning can be billed. API preview has no local response
cache; existing budget/cap reservations are copied to an in-memory ledger.
Subscription clients retain their own auth/cache behavior (`~/.claude`,
`~/.codex` or configured paths); Codex's existing temporary schema/output
protocol files are created in the system temporary directory and removed after
the call. No Weaver CLI work directory is created. Repeated previews do not
persist accounting reservations.

Apply requires `--plan PATH` and has no LLM client or model calls. After printing
a `cp -a state_root state_root.backup` instruction, it takes the daemon's
`weaver.lock` and verifies the saved Fabric head plus dictionary, summary and
Action outbox fingerprints. Changed inputs are refused before any data-store
write; re-plan and review again. `--yes` is required without a TTY. Never edit
the exported plan: its integrity digest is checked when loading it.

Each private journal unit persists the exact source batch, then adds dictionary
entries and automatic aliases before atomically appending system retractions
and new facts to Fabric. A failure can leave unused dictionary proposals, but
cannot append facts with missing dictionary entries. Completed sources are
skipped; normal failures checkpoint the actual state and replay exact IDs and
timestamps. If a process dies between side effects and checkpoint, the original
plan fails closed; re-planning recovers the prepared batch from the journal.
`--discard-plan --yes` clears pending units without deleting Fabric facts or
undoing dictionary edits (already appended batches become completed markers).
Take a new plan afterward. Future deliberate migrations must bump `RUN_KEY`.

Rejected reviews are excluded. The newest applied revision per source is
extracted with the existing `loom.approve` prompt through `ApprovalExtractor`;
all its active historical extraction Threads are replaced. Independent human
facts receive tag corrections only. `prepared.corrections` are intentionally
**not** passed to `context.learn`: this migration changes facts/tags, not the
reviewer's speech-correction history. Corrupt reviews are isolated to their
source; missing summary state and page errors are reported without tracebacks.
Any reported error makes the CLI exit nonzero, even if other sources finish.

A source referenced by an active external/human Thread through resolves,
blocked_by, updates, executes, retracts or correction.target is skipped in full:
`외부 참조로 건너뜀` lists the referencing seqs in text and JSON. This includes
human retractions of historical facts and later schedule updates from another
source, so they cannot be undone by migration. A source's own old schedules
are excluded from the approval model's existing_schedules; only server-side
old/new slot mapping sets updates.

Equal-slot replacements preserve calendar lineage. A chain rooted at or below
the Action activation baseline stays excluded, including changed slots; changed
legacy slots are listed as `캘린더 수동 확인 필요` for manual reconciliation.
Other schedules show 유지, update, remove+register, register or remove according
to the same Action derivation used by the engine. Review these effects before
applying. The next daemon start may cause the wiki compiler to recompile pages
whose tags changed, making additional LLM calls and incurring their normal cost.

## Approval extraction rules

`loom.approve` extracts business requests, decisions, schedules and records.
Review questions/answers, transcript interpretation, identity checks and
unconfirmed statements are not independent facts; answers only repair the
underlying business fact. This is a prompt rule, without text heuristics that
could delete valid business facts. Every involved person/organization, project
and topic belongs in the Thread tags; the record owner does not. Canonical
person names omit titles, affiliations and notes. Deterministic structural
checks drop malformed/titled proposed names, with content-free warnings.

`contact_name` comes from optional package metadata in `summary.sqlite3`
(`contacts`, keyed by package ID), saved from Source.hints.counterpart during
drafting. Legacy packages fall back to Spinner's stored Source metadata when
available; otherwise the input is null. No phone/schema change is needed.

Actual alternate names alone belong in aliases. Typographical errors and
misrecognitions belong in transcript corrections; spacing/case variants resolve
without aliases and are dropped from automatic proposals. A count-only warning
reports input dictionary people/organizations present in the approved body but
absent from every extracted Thread's tags; no name, body or source ID is logged.
Wiki human-edit extraction shares the dictionary budget, tag rules and proposal
validation, journals new entries before append, and leaves frontmatter changes
to code. Unknown generated tags are dropped rather than stored.

`open_requests` contains at most 50 active unresolved requests within an 8,000
UTF-8 byte JSON budget, ranked by shared relevant tags then newest sequence.
Each hint has id, content, tags, direction and occurred_at. Only an explicit
completion, answer or cancellation can produce a decision's `resolves`; similarity
is insufficient. The server drops targets outside the full active unresolved
request set. Re-extraction excludes the source's own old requests and schedules.

Re-extraction run `para-counterpart-v4` separates bootstrap `renames` from
`tag_corrections` (both observed → canonical). Only unedited learned
people/organizations may be renamed automatically; wiki and human names stay
protected. Apply uses `rename(..., human_edited=False)`, stores the former
spelling as a transcript correction, and adds no former-name tag alias.
Invalid/uncleanable names are omitted from automatic dictionary planning and
reported by count. Tag mappings are plan-only, including old typos and spacing
variants; independent human facts receive tag corrections. Only operations whose
canonical names are used survive into a source plan. Preview includes automatic
renames and tag mappings, and apply/retry journals both before Fabric append.
Synthetic replay and optional real-model evaluation are documented in
[`server/evals/README.md`](../../../evals/README.md).

## PARA area and exchanged party

Every newly extracted business Thread has exactly one `area` tag. Prefer the
few broad areas already present; propose an area only when none fits. Area hints
come first within the shared dictionary budget. The server rejects an extraction
with zero or multiple resolved area tags before journaling any facts, using
retryable `SchemaMismatch` and the existing bounded contract-error retry path. A project
must already exist and clearly fit; `new_entries` of kind `project` are dropped
with a content-free warning. Unedited Wiki project pages that have not been promoted are excluded from both
dictionary and recent-tag hints and cannot pass tag validation. Wiki-promoted
active projects are eligible; archived projects are always excluded. Bootstrap also
cannot create projects; curation/promotion belongs to the Wiki batch lifecycle. `topic` means PARA
resource. Wiki human-edit business facts use these same guards; tag corrections
and code-produced field history have their own contracts below.

`Thread.counterpart` is one canonical person/organization or null: the party the
work was exchanged with, usually the contact, requester for a received request,
or recipient for a sent request. The canonical counterpart is also added to the involved-party tags if omitted. The server validates it against the complete eligible dictionary plus
accepted new entries; invalid kinds/unknown names become null with a content-free
warning. An entry used only by counterpart is still persisted. Old Fabric rows
and cached plans read the missing field as null. Tag corrections change tags
only. Re-extraction's `para-counterpart-v4` plan stores area and counterpart and
prints counterpart in preview; older saved plan versions must be recreated.
The phone's `PipelineThread` projection deliberately omits counterpart for strict
older Android readers; the field stays in Fabric and the web API only.

Human Wiki frontmatter changes produce deterministic Korean history sentences
for assignees, deadline, schedule and status. Their IDs start `wiki-field:`;
semantic human facts still start `wiki:`. These field histories use the current
`state_tag` resolved through the full dictionary, plus the entity's area when a
single area is determinable from active related Threads, and counterpart is
always null. They never use the page filename as a tag. A missing/unresolved
state_tag leaves the edit in Wiki without a field Thread and logs a content-free
warning. New entries for semantic edits remain journaled before append.

Wiki semantic extraction uses its own rule block without approval Q&A or contact
owner instructions. Business facts require one area; tag corrections require
none. Invalid JSON, schema/area mismatches, missing output and refusals retry
per edit revision up to three times, with a durable attempt count. Exhaustion
journals an empty result, acknowledges the unchanged Wiki text, and emits a
content-free warning; later ticks/restarts reuse the empty result without a new
model call. Other edits remain independent. Re-extraction reports contract
errors per source. Count-only untagged-name warnings use emitted tags alone.

## Source time in approval and replay

Runtime drafts persist `Source.occurred_at or Source.captured_at` and whether the
reference is a fallback separately in `summary.sqlite3.source_times`, keyed by
package ID; phone contracts stay stable. Approval and re-extraction pass that
immutable `source_occurred_at`, `source_time_is_fallback`, configured `timezone`
and its local `today` to the shared prompt. Explicit dates and date headers in
the text take precedence over this reference when resolving relative dates.
`recorded_at` and summary creation remain processing times.

Legacy packages recover Source metadata from Spinner's `results.source`; if none
exists, the earliest saved package stage is the stable fallback, with a
content-free warning. A package with neither metadata nor stage timestamps is
refused instead of using today's clock. Re-extraction marks fallback references
as `출처 시각 추정` in its preview and `source_time_estimated` in JSON.

For a known occurrence reference, the server allows facts up to one day after the
source start (long calls), bounded by server now. Unsupported guesses later than
that or more than 366 days before the source are replaced by the source time,
clamped to server now, with content-free warnings. Invalid or past deadlines and
schedules before the source lose both field and value (and schedule update links)
with content-free warnings. Date-only deadlines compare local dates; timed
values without a zone use the configured timezone. A same-day timed deadline
before the source is dropped; a date-only deadline for that day remains valid.

Capture-only and stage-time references may describe imported history, such as
Kakao exports collected years after the conversation. For these fallback
references, the server preserves historical occurrence times, past deadlines and
past schedules, logging a content-free warning instead of applying the 366-day
or past-value guards. These dates rely on the model reading explicit transcript
dates correctly; the capture time cannot validate their historical accuracy.
Rows saved before provenance was recorded are conservatively treated as fallback
references. Invalid value types still lose their field/value, and every occurrence
after server now is clamped even when the phone's reference clock is ahead.

Re-extraction run `source-time-v5` supersedes `para-counterpart-v4`, so saved plans
must be recreated and completed older runs can be deliberately planned again.
Text preview includes occurrence and recording times alongside field values.
