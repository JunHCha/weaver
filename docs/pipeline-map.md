# Data flow map

The Android home screen shows where each captured item is and what it produced.
The map is an observation of local and synchronized state; opening it does not
approve content, call a model, or request permission to a new folder.

## Layout and actions

| Row | Phone | Mac | Output |
| --- | --- | --- | --- |
| Top | Common collection: Shepherd | Spinner | Fabric (on Mac) |
| Bottom | You (review) | Loom | Wiki (Obsidian vault) |

Shepherd is a shared collection role outside the phone region. Dashed regions indicate places. Courier is an icon on the phone–Mac transport
lines, rather than another storage node. The two review lines carry “엮은 내용”
(the summary document and at most three questions) and “승인” (the verdict), not
Threads. Loom extracts Threads only after approval and records them in Fabric.

Each item shows collection badges for every distinct capture host: this phone, Mac, another device, or unknown. Only local pending captures appear in the unpublished queue; remote queues are not observed. Capture hosts contain no device identifiers. Older records without an explicit host remain unknown.

Each item has one block. Fill color identifies its origin (call, voice memo, or
shared text); orange outlines indicate review due and red outlines indicate
`apply_stopped`. Only the review node has a task count, and only Loom has a
stopped count. Waiting for a Mac receipt is not a user task. Dense nodes show up
to twelve blocks and an overflow count; the node panel retains the complete item
list. Pinch and pan to inspect the map in a compact window.

Tap a block for its title and current state. Tap a node for its resident items.
Expanded landscape windows use a right panel; compact/portrait windows use a
bottom sheet. The review panel contains only outstanding reviews, grouped into
four display steps: 가져오기 · 다듬기 · 승인받기 · 저장하기. Expanding a card
shows the available stage timestamps and opens the existing review screen.
The tools entry retains the existing capture, Inbox, transport, and permission
controls, including notification entry points.

## Observation contract

[PipelineItem](../schema/PipelineItem.schema.json) is an additive version-2 wire
model at `proposals/pipeline/<source_id>.json`. Deploy the server first: a new phone manifest containing `capture_host` is rejected by older servers. Existing capture/receipt and
review/verdict contracts keep their versions. Old observations are ignored by
older clients; new clients retain the existing review-package fallback.

An observation links the capture and source to the latest review revision,
current node/status, stage timestamps, recorded node history, Thread IDs/kinds,
and committed wiki outputs. Each change gets a monotonically increasing
revision in a private server journal. `apply_failure_limit` carries the actual
configured retry threshold (five by default), so the stopped explanation remains
accurate when the server setting is customized. The synchronized file is a reconstructible
projection, not the source of truth. The phone keeps its latest observation
per source and Courier root; missing, malformed, or older synchronized files
do not delete it or roll it back.

A `Receipt` remains the authoritative Mac archive acknowledgement. The phone
retains receipt evidence independently of later transport cleanup. It displays
unsent captures under Shepherd and published captures awaiting acknowledgement
on the Shepherd–Spinner line. It does not infer completion from disappearing
review or transport files.

Stage timestamps describe observed milestones. Collection uses the capture
stamp, sending uses the archive receipt, and the remaining stamps are written
at processing milestones. Unknown historical stamps remain absent. Server
steps remain 수집 · 전송 · 정리 · 질문 · 검토 · 반영 · wiki갱신; the four card
steps group these rather than replacing the wire vocabulary.

The recorded node history preserves observed review round trips. Observation
sidecar failures never turn a successful core approval into a retry; a later
publisher tick recovers durable state. Missing historical observations stay
unknown instead of being synthesized. At a new visit the
phone compares its last-seen snapshot with the latest state and plays only new
transitions, once, with staggered starts. The first observation stays still;
rotation and panel changes retain the visit. “다시 보기” replays that visit's
paths. This is not a live transport animation.

## Wiki availability on the phone

Wiki output records contain a vault-relative Markdown path, publication timestamp,
and the SHA-256 of the entire committed file. The map lists today's page updates
and daily Digest. Fabric storage, wiki publication on the Mac, and arrival on the
phone are separate facts.

Configure the Obsidian vault name and select the **local synchronized vault**
folder on the phone. Weaver persists read access only and never writes to that
folder. A page is enabled only after its bytes match the published hash.
A missing folder grant, missing page, or old file remains grey with “폰에 오는 중”.
Selecting the Courier transport folder does not select the vault automatically.
Sharing the vault through Syncthing is a separate device setup task.

Links use `obsidian://open?vault=…&file=…`, encoding the vault and file components
separately, just like existing review wiki links. Obsidian must be installed and
have the same vault registered. Folder access alone does not register a vault in
Obsidian. Revoked folder access disables links until access is restored.

## Boundaries and validation

The map uses Compose Canvas paths, centered Material vector icons, per-block
Animatable movement, and tap popups; it does not depend on font glyph baselines.
The HTML design and screenshots stay outside the repository. Contract, state,
retry and UI tests use synthetic data, fake models/providers and temporary vaults.
Galaxy Fold and Syncthing transport validation on a real device remains separate
from automated verification; no actual call or user-vault content belongs in tests,
screenshots, issues or PR descriptions.

## Wiki-origin edits

Direct Wiki edits have teal blocks and a right-hand `Wiki → Fabric → Wiki` route.
They use `source_type: "wiki_edit"`, `capture_id: null` and the optional `wiki_edit`
metadata on PipelineItem. No capture, receipt, review request or review count is
manufactured. English domain names are used on the map: Shepherd, Courier,
Spinner, Loom, Fabric, Wiki, You and Thread; existing stored stage/kind values are
unchanged.

A pending edit stays at Wiki until its contents settle. Confirmed semantic or
field changes move to Fabric only after their Thread IDs actually exist there.
The result moves back to Wiki only after compilation has advanced and committed
outputs are available. The details show changes, generated Threads and result
links; phone arrival is still checked independently by hash. Historical Wiki-edit
outputs are checked even when they were produced on an earlier day.

The source ID identifies a path/base-revision edit round, so continued typing
updates one waiting item. Reverting to the baseline or formatting-only edits close
without a Fabric transition. Durable edit history, generation drafts and actual
Fabric/output state recover failed observations; an observation failure does not
roll back a fact. Missing old acknowledged edit history is not invented.

Capture observations omit the new optional fields on the wire so existing strict
v2 apps can still read them. Older apps skip Wiki-origin observations; updated apps
accept old capture observations without the new defaults. Use an updated Android
build to see the new route and detail tabs.

## Action outbox and calendar execution

Approved schedules reach the calendar through the Action path, not through a
file the user taps. `Thread.kind` is a fact and lives in Fabric; `Action.kind` is
a command and lives in the Action outbox (`state_root/action-outbox.sqlite3`,
`weaver.action`). A command is never a Thread kind.

1. The Action engine reads Fabric by a durable `seq` cursor. When there are new
   rows it judges the schedules with `loom.schedules.approved_schedules` (blocked
   by an open request, retracted, resolved) and enqueues what is still needed. The
   enqueue and the cursor commit are one SQLite transaction, and a unique
   idempotency key (`<kind>:<event>:<position>`) makes a replay or a rewound cursor
   a no-op. A new schedule becomes `calendar.register`; a later schedule of the
   same event with another time becomes `calendar.update`; retracting every live
   schedule of an event becomes `calendar.remove`. An event is a chain of schedule
   Threads with the same title (case and spaces folded) and at least one shared
   tag; unlike the alert detector, untagged Threads never join, so an automatic
   change cannot move an unrelated event that only has the same name. A blocked
   schedule is held and never executed; blocking does not remove an event that
   already exists.
2. Actions of one event run in order. The server publishes `actions/<action_id>.json`
   only for the next one, with the calendar event ID once the register is done.
3. The phone executes by itself in the Courier worker. The target calendar is
   chosen once in the app; until then Actions wait and nothing is written. The
   fsynced pending marker and the recovery rules of the earlier manual
   registration are kept (`CalendarJournal`): an ambiguous or unreadable calendar
   never produces a second event. Transient failures retry with backoff, five
   attempts in all, then the Action is given up. Missing calendar permission or a
   removed calendar is a wait, not a failure. The app shows each Action's state
   and has no registration button.
4. The phone reports only a final result in `action-results/<action_id>.json`.
   The server appends one `실행` Thread (`executes` names the Thread the Action
   came from, `actor` is `system`) and then updates the outbox. Replaying a result
   is idempotent, so a crash between the two steps only repeats harmless work.
   The outbox is derived: a lost outbox is rebuilt from Fabric, and an Action whose
   `실행` Thread exists comes back already finished, so it does not run twice.
   Intermediate edits of an event are not replayed from history on a rebuild.
5. `실행` Threads are not business facts. Fabric readers skip them unless asked;
   wiki, Digest, query pages and this map list only facts.

### Rollout and coexistence with older apps

Deploy the **server first**, then update the phone app.

- The new server stops writing `proposals/schedules/` and removes the files it
  wrote there. It publishes Actions in two new top-level Courier folders,
  `actions/` and `action-results/`, which older apps never open.
- An outbox created beside legacy schedule files starts after everything already
  approved (its baseline is the Fabric head at that moment). Schedules approved
  before the upgrade are not registered automatically, because the old manual flow
  may already have put them in the calendar. A fresh install has no baseline.
- Older phone app with the new server: review, alerts and this map keep working
  and nothing is rejected. The app finds no schedules, so it offers no
  registration until it is updated. Old apps read only the six existing Thread
  kinds on this map, and the `실행` Thread is never listed there.
- New phone app with an older server: it finds no `actions/` files and does nothing.
  The old server's `proposals/schedules/` files are ignored.
- Only one phone should run the executor; ownership across several devices is not
  decided yet.

The ADR-039 first step stops here. A human moving or deleting an event in the
calendar is not detected, and to-do and reminder Actions are not defined yet.
