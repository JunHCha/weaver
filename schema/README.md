# Weaver JSON contracts

These standalone JSON Schema Draft 2020-12 files are the phone/server contracts.
Each document contains its own `$defs`; every `$ref` is document-local. `$id` is
an identifier (`urn:weaver:schema:<Model>`), not a URL to fetch. Consumers should
enable `format` checking for `date-time`. Schemas are generated in **validation**
mode with aliases. JSON Schema is for validation; write Kotlin models from each
file's top-level `properties` and follow its referenced nested definitions. Do
not treat `$defs` or `if/then` branches as additional wire envelopes.

Regenerate from the repository root:

```sh
uv run --project server python -m weaver.schema.export --output schema
uv run --project server pytest server/tests/schema -q
```

`examples/<Model>.json` is the minimum complete shape of each model. All data is
synthetic.

## Models

| File | Direction | Version | Purpose |
| --- | --- | --- | --- |
| `Manifest` | phone → server | 1 | One capture's metadata; the body is a sibling file. |
| `Receipt` | server → phone | 1 | Acknowledges a stored capture. |
| `Source` | server | 1 | Archived capture metadata. |
| `Script` | server | 1 | Parsed text and original segment provenance. |
| `ReviewPackage` | server → phone | 2 | One source's summary draft and up to three questions, one file per revision. |
| `Question` | in `ReviewPackage` | - | A question with exactly three choices. |
| `Verdict` | phone → server | 2 | `approve`, `edit`, `feedback` or `reject` for one exact package revision. |
| `EventSummary` | server | 2 | The approved, content-addressed summary document. |
| `ThreadDraft`, `Thread` | server | - | A Fabric row before and after `seq` is assigned. |
| `ApprovedSchedule` | server → phone | 2 | A schedule projected from Fabric for calendar registration. |
| `PipelineItem` | server → phone | 2 | Durable source location, transitions, stage times and committed Thread/wiki results. |
| `Alert` | server → phone | 2 | A notice that a newly approved schedule moved an approved one. |

## Common rules

- Capture files (`Manifest`, `Receipt`, `Source`, `Script`) carry integer
  `schema_version: 1`; review, summary, schedule, alert and pipeline files carry `2`.
  Consumers reject any other version, and the server rejects versions older than
  the current ones without migrating data. Fabric rows carry no version key; the
  Fabric database itself is versioned.
- Unknown keys are forbidden. Every declared key is required, including nullable
  keys. `null` means unknown or not applicable. Empty collections are `[]`.
- IDs: `cap_`, `src_` and `alt_` plus a canonical uppercase ULID (26 Crockford
  characters, first digit 0-7). Package, thread and summary IDs are opaque
  strings; a summary ID is the SHA-256 of its content.
- Timestamps use RFC 3339 with an explicit timezone. Numeric epochs are not wire
  dates. `sha256` is 64 lowercase hexadecimal characters; integer fields require
  JSON integer tokens (`1`, not `1.0`). JSON Schema cannot express that token
  rule, so consumers enforce it while decoding.
- `payload` / `payload_ref` are safe logical relative keys, never local paths.

## Review files

`ReviewPackage` files are `<package_id>.r<revision>.json` in the `proposals/`
Courier folder; the phone answers with `<package_id>.r<revision>.json` in
`decisions/`. A verdict is valid for one revision only: `edit` requires
`edited_body`, `feedback` requires `feedback`, `approve` answers each question
with one of its choices. `stage` and `stage_times` show the processing steps
(수집, 전송, 정리, 질문, 검토, 반영, wiki갱신).

## Threads

Kinds are `요청`, `결정`, `일정`, `기록` plus two system rows, `태그정정` (a tag
correction naming its target Thread) and `거절` (no content). Only requests carry
`direction` (`받음`, `보냄`). `field` is one of `deadline`, `schedule`,
`assignees`, `status`, and `value` is arbitrary JSON, except that `schedule`
requires `{start, end, timezone}` with an IANA timezone and `end` after `start`.
`actor` is `ai_approved` or `human`. A request stays open until a `결정` Thread
names it in `resolves`; a schedule may name the request that blocks it in
`blocked_by`. Approval records these facts and never authorizes executing a
request.

## Script

Segment `[start_char, end_char)` indexes Unicode code points of `Script.text`, not
bytes or UTF-16 units (Android must convert). Segment IDs are unique within a
Script. A known `start_ms` permits a null `end_ms`; a non-null `end_ms` requires
`start_ms` and must be greater. Cross-value checks beyond JSON Schema are
repeated by the Python models and the Kotlin classes.

`ArchivedSource` (server-only, with the local payload path) is not exported.

## Pipeline observations

`proposals/pipeline/<source_id>.json` is an additive `PipelineItem` projection;
existing Manifest/Receipt/ReviewPackage/Verdict shapes and versions do not change.
The phone retains the highest source revision rather than treating a missing file
as success or deletion. A private `pipeline.sqlite3` preserves revisions and
location history across server restarts and can republish deleted projections.
`capture_id` and the existing Receipt join local captures to a Source. `history`
records observed location transitions, including repeated `loom`/`user` feedback
visits; absent old observations are not reconstructed from guessed timestamps.

`stage_times` records capture time (수집), the durable Receipt's receive time
(전송), summary/questions completion and review-wait entry (정리/질문/검토),
durable approval application (반영), and the committed wiki output's file time
(wiki갱신). Unrecorded historical steps remain absent. Processing and approval
remain authoritative if the observation sidecar temporarily fails; the publisher
reconciles durable journals on a later tick. Such a failure can leave intermediate
observations absent, so consumers must not invent those times or transitions.

The configured consecutive matching approval failure limit (default five)
publishes `apply_stopped` in the same scan. `apply_failure_limit` records that
actual limit, so the phone does not mislabel a custom configuration as five. A server restart may retry the existing verdict but does not clear the
observed stopped state until application actually succeeds. A newly reviewed
revision can return to `user` normally.

`threads` exposes IDs and kinds; `pages` and `digest` contain safe vault-relative
`.md` paths, full-file SHA-256 fingerprints and update times. Outputs are observed
only from committed tracked wiki versions; a pending human edit is not advertised
as a generated result, and the last confirmed output remains available. The phone
compares the hash with its SAF-accessible vault file before enabling a link. A
daily Digest can concern an older occurrence day even when updated today; only a
basename matching today's `YYYY-MM-DD.md` is labelled today's Digest. The map's
"today" update filtering uses the phone's local date. Wiki publication follows
its existing maintenance interval; the next observation tick exposes new output.
