# Wiki

## Human edit detection

`WikiStore.scan` discovers and registers only files with the `.md` suffix.
Other files, including binary attachments and text/JSON files, are silently
ignored and cannot become human edit candidates. The suffix is case-sensitive:
`.MD` files are ignored too. On startup, existing non-`.md` rows (including `.MD`)
are removed from `pages`, `pending` and `human_edits`; files and git history are
left untouched, and previously confirmed non-Markdown edits are never delivered.
Missing Markdown files keep their existing rows and detection behavior.

## Project lifecycle

`BatchCompiler` requests `promotions` alongside pages and digests in its existing
single `wiki.compile_batch` call. Each item names the project, area, goal, end
state, whether it is a one/two-day task, and evidence Thread IDs. The model judges
semantic identity and goal/end state. The deterministic rule is **the same area
and (two or more sources OR two or more local dates) and a clear goal/end state**.
Each evidence Thread needs exactly one resolved area tag, and all must resolve to
the proposed area. Dates use the configured timezone injected by `WikiJob`;
there is no additional elapsed-span requirement. A short processing task stays
in the area's operation page. A page's `role=project` alone is not a promotion.

The batch accepts loose JSON promotion items and validates each independently in
`ProjectLifecycle.apply`. Bad lengths/types, repeated evidence IDs and duplicate
normalized project names produce content-free skip reasons. Valid pages, digests
and other promotion items still publish. An evidence Thread already tagged with
one active project belongs to that existing project: new evidence is attached
there, and no competing project is created. Evidence with multiple active
projects is ambiguous and skipped.

After publication and dictionary sync, the compiler journals valid promotion and
correction payloads, then advances the bounded input bookmark before replaying
Fabric side effects. Every tick, including a tick with no new input, replays
pending `actor=system` corrections with stable `PROJECT_THREAD_PREFIX` IDs and
prepared timestamps. It registers the dictionary project without setting
`human_edited`. Original Fabric rows never change. Failed or partial appends do
not repeat the LLM call. The cache signature excludes dictionary lifecycle data
and the compiler's own generated project tags; digests likewise use the stable
input snapshot, so promotion effects do not invalidate cached generation.
`RULES_VERSION=work-vault-v3` and the new batch prompt cause one regeneration of
existing digest periods when the next batch plans those periods.

Rename, human retraction of a promotion correction, and explicit project-tag
removal durably veto that project's promotion; alias and kind edits do not.
Removal labels are resolved through the dictionary before comparison. Vetoes
match the normalized project name or the same area with overlapping evidence
`source_id` sets, even after re-extraction changes Thread IDs. A different
established project keeps its independent history even if it shares evidence.
Retraction/removal sets `promoted=False` and a durable `promotion_reverted` flag,
excluding that project from extraction hints and validation even if its entry is
learned or human-edited. Renamed dictionary entries keep historical resolution.

The archive activity timestamp is the latest `min(occurred_at, recorded_at)` of
active business facts: re-extraction arrival does not refresh old source events,
and a future occurrence cannot push activity beyond its recording time. A group
already inactive for exactly 30 days cannot be newly promoted. Only promoted
projects auto-archive after 30 days; human-created projects archive only on
`완료`, `completed` or `done` status. Archive checks also run without new input.
A new business Thread tagged with an archived project automatically unarchives
it; a saved archive sequence prevents an older completion status immediately
archiving it again. No web unarchive action is provided.

Dictionary lifecycle flags preserve human provenance and survive wiki sync.
Read-only readers accept earlier v1/v2 dictionaries without lifecycle fields;
writable initialization adds the tables/column transactionally. All lifecycle
and batch SQLite connections close explicitly. Archived tags remain readable in
history and the dictionary but are excluded from extraction candidates. The web
project grouping hides archived project lanes by default, offers a display toggle,
and puts facts whose project tags are all hidden in the `프로젝트 없음` lane.

`server/tests/wiki/test_promotion.py` uses only synthetic data, fake LLMs and
private temporary stores, covering mixed valid/invalid outputs, Seoul midnight,
source-time archive boundaries, append replay, reverts and independent projects.

## Read-only contact view

`사람별 연락.md` counts active business Threads by their single canonical person
`counterpart`, resolving dictionary renames and aliases. Mention tags, organization
counterparts and missing counterparts do not count as contact. Lines are ordered
by the canonical counterpart name in ascending Unicode string order for stable
rendering, not by latest contact time. Each line separately shows its latest
contact date and the preceding 7-day/30-day counts in the injected local timezone.
