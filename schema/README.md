# Weaver JSON contracts v1

These ten standalone JSON Schema Draft 2020-12 files are the Android/Courier
contracts. Each document contains its own `$defs`; every `$ref` is document-local.
`$id` is an identifier (URN), not a URL to fetch. Consumers should enable `format`
checking for `date-time`. Schemas are generated in **validation** mode with aliases
(`from`, not the Python attribute `from_ref`). JSON Schema is for validation;
write/generate Kotlin models from each file's top-level `properties` and follow
its referenced nested definitions. Do not treat `$defs` or oneOf/if/then branches
as additional wire envelopes.

Regenerate from the repository root:

```sh
uv run --project server python -m weaver.schema.export --output schema
uv run --project server pytest server/tests/schema -q
```

`examples/<Model>.json` provides the minimum complete shape for each core model.
Additional examples cover four Verdict branches, human request resolution, output
provenance and Source→Thread for text, Kakao and audio. All data is synthetic.
The examples deliberately include a request and a blocked schedule; approval of
that proposal is not calendar execution permission.

## Common rules

- Every core object has a required integer `schema_version: 1`. Consumers reject
  unknown versions with a diagnostic. Nested value objects inherit their enclosing
  file version: nested entities/relations/schedules/drafts have no schema_version.
  Standalone Entity and Relation files retain their top-level version key.
- Unknown keys are forbidden. Every declared key is required, including nullable
  keys. `null` means unknown/not applicable; omit nothing. Empty collections are `[]`.
- IDs are prefix + canonical uppercase ULID: `cap_`, `src_`, `prp_`, `alt_`, `ent_`,
  `evt_`. ULIDs have 26 Crockford characters and first digit 0–7. Local refs are
  one lowercase letter followed by a positive integer (`e1`, `s1`, `d1`, `t1`).
- Timestamps use RFC 3339 with explicit timezone (e.g. `2026-11-07T15:32:00+09:00`);
  nullable occurrence times preserve uncertainty. Numeric epochs are not wire dates.
- `sha256` is 64 lowercase hexadecimal characters; sizes/offsets are nonnegative
  integers; rev ≥ 1; confidence is finite and in [0,1]. Integer fields require JSON
  integer tokens (`1`); `1.0` and `1e0` are forbidden even when mathematically
  integral. JSON Schema treats these as integers, so consumers must enforce this
  token rule during decoding in addition to schema validation. Regression tests
  explicitly document this remaining lexical boundary for schema_version/rev.
- `payload`/`payload_ref` are safe logical relative keys: alphanumeric, `_`, `-`,
  slash-separated directories, optional final extension. No absolute path, colon,
  backslash or `..`. Only internal `ArchivedSource.payload_path` holds a local Path.

## Confirmed ontology (issue #22)

Entity types: `person`, `org`, `project`, `topic`, `meeting`.
Relation types: `원인`, `후속조치`, `같은논의`, `참석`, `소속`.
`결정됨` is excluded: decisions are Entry facts, not a duplicate relation kind.
Old `events`, `decision`, and array-dot edit paths are not v1 wire fields.

Entity `ref` addresses a proposal-local item and nullable `entity_id` links a known
entity. Standalone entities need at least one identity; proposals need unique refs.
Aliases are names, not automatic identity merges. `confidence` covers the proposed
item including its match; `ask: {question, choices: [{entity_id, label}]}` or `null`
asks for confirmation. Choices may be empty; a free-text answer is allowed.
Entity, relation, schedule and draft include `thread_excerpt` evidence.
Relation `from`/`to` use declared refs or known entity IDs; `this` means the source.

Proposal `source` is a path-free summary of Source. Local refs are unique across
entities, schedules and drafts. `schedules[].entry_ref` points to an `일정` draft.
Proposal validation rejects duplicate/dangling references and mismatched blocks.

## Draft → Entry and requests

`entry_drafts` holds proposed facts, not execution commands. Kind is one of
`요청`, `논의`, `결정`, `폐기`, `약속`, `일정`, `변경`. `field` is nullable;
`value` is arbitrary JSON, never a Python object. `supersedes` and `retracts` can
express existing Entry links; ordering and state folding belong to Fabric.

Exactly `kind=요청` requires non-null `request` with `requester`, nonempty `targets`,
nullable `due_at`, and `content`. Other kinds require `request: null`. A request
remains open until a user-authored `kind=결정`, `actor=human` Entry links to its
`evt_` ID through `resolves`; otherwise `resolves` is null. A decision extracted
from source material and approved as a fact is not an explicit user decision to
execute a request: use `ai_approved` and do not set `resolves` for that approval.
Fabric verifies that an external `resolves` target actually is a request.

Producers **must** mark all request-derived drafts with `blocked_by_request: dN`
and propagate it to the corresponding schedule. A schedule cannot remove its
draft's block. `Verdict approve/edit` records the existence of requests and other
facts; it does not resolve requests or permit derived actions. Execution must wait
for an explicit human decision Entry. The block persists in stored Entry as the
request's `evt_` ID, so it survives proposal removal. There is no executable
`actions` collection in v1.

`weaver.schema.internal.materialize_entries` implements the mapping: the caller
allocates one `ent_` per new entity ref (preserving existing matches), one distinct
`evt_` per draft, and supplies recorded_at. Newly allocated entity IDs must be
unique and disjoint from all existing matches; intentional existing matches may
share an ID. It replaces entity refs in tags/request fields and draft refs in
blocks, injects source_id, and validates each final Entry. Every resulting Entry
has actor=ai_approved and resolves=null; there is no actor override. EntryDraft
resolves accepts only null, including in the JSON Schema. A source-derived decision
can be recorded as a fact but cannot resolve a request through Proposal approval.

`internal.create_request_decision` is a separate creation path for an explicit
human decision on an existing request Entry. It sets actor=human and
resolves=request.entry_id and rejects non-request targets. Call it only for an
actual user decision, never from Proposal approval. Both paths return facts and
perform no execution or storage.
Entry `source_id` also permits nonempty `output:<logical-output-reference>`.

## Verdict branches

All four branches require proposal_id, rev and reviewed_at. Other inputs are:

| verdict | edits | reject_reason | instruction |
|---|---|---|---|
| approve | `[]` | null | null |
| edit | nonempty list | null | null |
| revise | `[]` | null | nonblank string |
| reject | `[]` | required enum | null |

Reject reasons: `wrong_person`, `wrong_content`, `duplicate`, `not_useful`, `other`.
Each edit is `{path, value}`; path uses RFC 6901 (`/schedules/0/start`, `~0` for
`~`, `~1` for `/`). The empty pointer denotes the whole document. The handler
must resolve pointers, restrict editable fields, and revalidate the complete
Proposal after edits, including execution blocks. Edits to blocked_by_request,
request, and resolves paths are forbidden, including descendants and replacements
of ancestors (arrays/objects/the whole document) that could overwrite those fields.
The handler must decode pointer escapes before enforcing this rule and preserve
the protected structure when validating a replacement. Request corrections use
revise rather than editing these protected fields. VerdictHandler implementation
and enforcement are a follow-up issue; syntax validation alone is not permission
to apply an edit.

## Thread and server boundaries

Thread segments retain source_id through their parent and use unique segment IDs.
`[start_char,end_char)` indexes Unicode code points in Thread.text, not bytes or
UTF-16 indices (Android must convert indices accordingly). Kakao retains speaker,
message occurred_at and text position; audio uses paired start_ms/end_ms offsets
from payload start. Text segments can have null times/speaker. Python additionally
checks nonempty ordered ranges and bounds, time ordering, local reference integrity
and block propagation, which JSON Schema cannot express across values. Clients
should retain these fields without inferring missing times or speakers.

`Candidate`, `EntityIndex`, `Extraction`, `ArchivedSource` are server-only and not
exported. `Extraction` is the generate_json boundary: it has no proposal ID, rev,
source metadata or server version. Its `to_proposal` injects server context and
runs the same Proposal validation used for Courier files. Subordinate value shapes
are reused; LLM output cannot bypass the final wire validation.

The full Extraction JSON Schema reuses structural allOf/if/then and regex (`\S`)
constraints. Some LLM structured-output providers do not accept this schema dialect.
The follow-up Extractor implementation must supply a provider-compatible simplified
schema, then validate/convert its output through Extraction and Proposal. This PR
does not claim the full schema can be passed unchanged to every provider.

The dev dependency `jsonschema[format-nongpl]` installs the optional date-time
checker as well as JSON Schema validation. Tests compare Pydantic with
Draft202012Validator(FormatChecker) for Verdict branches, Entry kind/request/
resolves/actor, entity identity, proposal refs, segment ms pairs and timestamp/ID
constraints; exported schemas and all examples are also validated independently.
