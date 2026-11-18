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

`BatchCompiler` requests `promotions` alongside affected page sections and digests
in each bounded `wiki.compile_batch` call. Each item names the project, area, goal, end
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

## Human pages and private metadata (#166)

`pages/*.md` publishes only nonempty `deadline`, `schedule`, `status`,
`assignees`, `next`, `area`, `aliases`, `tags`, `cssclasses`, `date`, and the `weaver` page connection ID.
Page roles, state tags, source/Thread IDs, section-to-fact assignments and hashes
are JSON documents committed under `meta/` in the private `state/wiki.git` tree.
No metadata worktree files are created in the vault, and `published_pages()`
continues returning the same path/bytes/timestamp contract used by PipelinePage.
Source links have readable display labels. The final `## 내 메모` and everything
after it are human-only, including any further headings; generation never sees
or replaces that text. Queries and digests carry an automatic-generation notice
and are restored from their committed baselines if edited.

Initial generation advances through at most 32 new Threads per tick. Later
calls use the section evidence map to find affected sections with SQL tag and
counterpart matches; FTS5 supplements missing exact matches. The evidence cap is 64 facts **per section**. Above that cap, merge-only mode
supplies the existing section and new Threads and keeps old wording and evidence.
Source excerpts have a 32,000-character total budget (12,000 per source).
Only selected sections are supplied and replaced; other sections keep their
exact text. The model supplies `section_threads` for precise section evidence;
older cached/model responses without that map conservatively associate the
page's supplied evidence with its sections. Selected sections carry exact human wording. Stale wording is retained with a
deterministic `⚠ 최신 기록: …` line; it does not block newer facts. Two consecutive
validation failures mark the section `검토 대기`, skip it and advance the bookmark. Generation cache and write
hash/quiet-period checks still protect retries and concurrent human edits.

Human revisions store their exact before/after bodies alongside their Git commit
IDs. Code divides edits by section and line hunk before any model call:
frontmatter values produce the existing value-change Threads, memo hunks are
saved, whitespace/formatting hunks record expressions, and private fields/managed
evidence are restored. Content hunks use one structured call per page revision,
with an explicit fact/structure/expression verdict for every hunk. Facts produce
Threads, structural verdicts update section assignments, and expressions remain
in the revision history. `edit_hunks` in the compiler database records the commit,
page, hunk, classification and before/after text for future writing-rule proposals.
Contract errors retry under the existing bounded edit retry policy.

## Explicit migration

Stop the resident server before applying an existing vault migration. Preview,
backup, and application are separate from normal compilation; implementation
and tests never apply to a real vault automatically:

```sh
uv run --project server python -m weaver.app wiki-migrate --config /path/server.toml --preview /outside-vault/wiki-plan.json
# Read every diff and metadata change in wiki-plan.json, then explicitly confirm:
uv run --project server python -m weaver.app wiki-migrate --config /path/server.toml --apply /outside-vault/wiki-plan.json --backup /outside-vault/new-backup --yes
```

Application takes the compiler lock, rejects stale previews, preflights all
files, and saves the original files, private Git repository, SQLite backup and
plan before publication. Backup destinations must be new and outside both vault
and server state. Repeating a completed migration has no changes. An interrupted
application keeps its complete backup; generate a fresh preview before retrying.
Migrate legacy pages before relying on section selection for existing content.

`server/tests/wiki/test_sections.py` covers partial publication, exact human
wording and memo preservation, code/model hunk call counts, machine restoration,
private metadata publication boundaries, SQL/FTS selectors, migration safety and
idempotency. The synthetic input comparison uses the same UTF-8-bytes/4 token
estimator on both sides; it is not provider token billing or a real-vault benchmark.

## Review corrections and identity policy

Selection excludes dictionary entries of kind `area`; common areas cannot select
all sections. Topic/person/organization/project keys, counterpart, direct Thread
references and auxiliary FTS5 determine scope. Missing section keys are dropped;
heading renames and order-only moves are code-classified structural edits. A page
without a section map gets a heading bootstrap. A migration section without a
reliable Thread match is marked `미분류` with `thread_ids=[]`.

The `weaver` ID is stable across filename/path moves. Move detection transfers
private metadata and the tracked baseline without an LLM or new Thread. A copied
ID gets a fresh identity and an empty map; it never inherits the source page's
metadata. Changing a tracked file's connection key is ignored and restored.
The filename change does not automatically rename business tags in Fabric.

Code-classified frontmatter/memo/expression/metadata/structure hunks commit before
the content-model call. Model outages leave only content hunks pending. Missing
state_tag is inferred from a single area in the page's evidence; unresolved value
changes stay pending. Hunk payloads include produced Thread IDs. Public fields
come from deterministic Thread projections; model aliases do not replace human
frontmatter. The code fills `area` from the unique area tag.

Empty scope queues unmatched Threads. Three queued Threads or seven days in the
queue allow a new-page proposal, including when no new Thread arrives that tick.
Models can return `pages=[]` and `new_sections` rows with `page`, `after_section`,
and `body`; existing sections remain untouched. When selected sections exceed
half the page, or estimated prompt input exceeds 60K tokens (UTF-8 bytes / 4), the
compiler logs a page-level full rewrite fallback. Memo and human wording protections
still apply; oversized evidence sections retain merge-only protection. Promotion
eligibility and duplicate checks receive the full active Fabric in code, while
the prompt receives compact promotion candidates. Digest inputs retain the full
period and are never narrowed to page-section evidence.

Before migration, drain human edits with `run` and inspect the preview. File/baseline
hash differences, candidates, pending/human-edit rows, and a mismatched legacy
`weaver_content_sha256` cause `먼저 컴파일러로 처리` skips at preview and apply.
Rows that remain in the human-edit history still cause a conservative skip; the
converter never acknowledges or absorbs them. Per-page parse errors are listed
as `변환 불가`; other pages remain reviewable. Preview lists keys moved into meta.

Rollback: stop the resident server; first save the current vault and state in a
new external directory to retain changes made after migration. Restore the
backup's `files/` tree into the Wiki folder. Move the current `state/wiki.git`
and `state/wiki.sqlite3` aside, then copy the backup's `wiki.git` and
`wiki.sqlite3` into those paths. Do not roll back Fabric or remove newer user
files. Inspect the restored files and pending edits before restarting `run`.

## No-model measurement

```sh
uv run --project server python -m weaver.app wiki-measure --config /path/server.toml
uv run --project server python -m weaver.app wiki-measure --config /path/server.toml --after-seq 0
```

This command uses read-only Wiki/Fabric/context connections and a temporary SQL
index. It does not scan, bootstrap, advance bookmarks, initialize writable stores,
or call an LLM. JSON reports each 32-Thread cycle's selected sections, compile
mode and old-full versus new-partial **page-payload** bytes/token estimates.
The estimator excludes provider billing, system-token overhead and full-period
Digest payloads; it is an offline planning comparison, not a paid model run.
The synthetic comparison includes four shared areas, one area per Thread,
overlapping counterparts and skewed common topics rather than isolated tags.
