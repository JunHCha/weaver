# Webex bundle contract

`WebexBundle` in `webex.py` defines Weaver's UTF-8 JSON payload, identified by
`format: "weaver.webex.bundle.v1"`. It is an internal archive format, **not** an
assumption about Webex REST fields. The synthetic fixture is
`server/tests/spinner/fixtures/webex-bundle.json`; real pagination, REST filtering,
reply coverage and deleted-message semantics remain for the poller work.

The envelope contains `instance_id`, `space_id`, and nonempty `messages`. Messages
contain unique `message_id`, optional `parent_id`, nullable `author_display`,
aware RFC 3339 `created_at`, optional `updated_at`, `text`, and attachment metadata
(`name`, `media_type`, `url`). Unrecognized fields/formats and empty messages without
attachments fail closed with content-free diagnostics. This format carries source
identity in archived bytes so content dedup cannot merge equal text from different
Spaces/instances. Payload instance identity is provenance, never authentication.

The built-in `WebexParser` sorts by UTC creation instant then message ID, keeps
Unicode text and display speakers, and makes one Script Segment per message with
code-point offsets and creation timestamps. Attachment-only messages become a
visible undownloaded marker. Attachment names/URLs and Space/message/parent IDs
never become Script fields; full metadata remains in the archived payload and
private provenance. Attachments are never fetched. No Fabric rows are created.
`TextSpinner` remains the legacy Kakao/share-text adapter; use durable `Spinner`
for Webex, so provenance commits before the parsed-success checkpoint.

`WebexProvenanceStore(state_root)` writes `webex-provenance.sqlite3` (0600) outside
shared schemas and vault projections. `get(source_id)` returns sorted message
provenance; `resolve(source_id) -> tuple[str, str] | None` returns
`("webex", space_id)` or None. Writes are immutable and idempotent by archive Source
ID, with conflicts rejected, FULL synchronous commits and macOS fullfsync. A crash
between private commit and Spinner success replays the same record. This matches
B2's `ProvenanceResolver` structurally without importing its not-yet-merged policy
module; the later merge wires this resolver into the policy gate. Parsing success
is not external-LLM authorization.

Deploy the updated APK before emitting `webex` Sources, PipelineItems or Scripts;
older strict enums reject the new channel. No shared field was added and existing
schema versions stay unchanged. OAuth, grouping dates/parts/revisions, capture IDs,
polling and LLM gates belong to later/separate work.

Closed Space-day bundles (webex stage 7) additionally carry optional `day`, `part`,
`part_count`, `revision` and `corrects_capture_id`; collector buffers omit them
(defaults). They are copied into private `WebexProvenance` only, never into the
Script, and review titles show `(n/m)` for parts and `정정 N` for corrections.
