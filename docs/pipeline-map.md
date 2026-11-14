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
