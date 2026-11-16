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
