# Shepherd registration and capture delivery

`weaver shepherd serve --config config.toml` starts a separate HTTP loopback
listener, using `[shepherds.courier]` (127.0.0.1:8788 by default; ::1 is also
supported). It reuses the existing `/mobile/v1/captures` payload PUT, Manifest
PUT and Receipt GET routes and ArchiveStore journal. Receipt still means
archive storage, not parsing, LLM permission or review completion. The phone
HTTPS listener always retains real-peer whois and node/user binding. Local
credentials work only on the dedicated loopback listener; tailnet credentials
work only on the whois listener. Proxy headers are disabled on both.

Register without printing a token:

```sh
uv run --project server python -m weaver.app shepherd register mac-webex-primary \
  --token-file /absolute/private/secrets/capture.token --config config.toml
uv run --project server python -m weaver.app shepherd serve --config config.toml
```

The token file's parent is private 0700, the file is 0600, and the server stores
only its hash in the existing mobile device journal. CLI validation rejects
sync/vault overlap and symlinks, including resolved parent aliases. TOML stores
the path (`shepherds.webex.courier_token_file`), never the token. Registration
creates a stable instance-to-device mapping and grants only `capture`; no
read/review/action/edit scopes. `--transport tailnet --node-id ...` optionally
adds `--login-name ...` for an operator-confirmed tailnet binding. Tailnet ACL
changes remain an explicit operator step, limited to the instance's node/port.
`shepherd revoke INSTANCE` revokes immediately. Repeating register rotates the
credential atomically and preserves instance/device/capture IDs, even after
revocation. A crash between credential publication and DB commit fails closed;
repeat registration to repair it. Webex OAuth is a separate credential lifecycle.

`weaver.shepherd.outbox.Outbox` is the shared synchronous library for collectors.
Open one private spool per stable instance at
`state_root/shepherds/INSTANCE/outbox.sqlite3`, passing the TOML-derived
`config.shepherds.courier.limits`. It uses SQLite FULL synchronous commits and
macOS fullfsync; it does not access a source API or advance a cursor itself.
`buffer(BundleKey(...), payload, cursor=(name, value))` commits source bytes and
cursor together. For a multi-item source page, commit all items before attaching
the page cursor to the last buffer write. An interrupted partial page is replayed
with the old cursor. Call `close(key, manifest, depends_on=...)` once to freeze
payload and Manifest bytes. Retries never regenerate timestamps, sorting or IDs.
The initial revision is 0; corrections use a higher revision and require a
closed original/prior capture dependency. Dependencies never become sendable
until the predecessor's verified Receipt is durably committed. Discarded and
quarantined predecessors keep corrections blocked; unrelated captures continue.

`BundleKey(instance_id, space, ISO-date, part, revision)` normalizes text to NFC,
serializes five UTF-8 fields with four-byte big-endian byte lengths, takes the
first 128 SHA-256 bits and encodes 26 uppercase Crockford Base32 digits after
`cap_`. The tuple journal detects collisions instead of overwriting a request.
Part and revision must be durable collector decisions, not recomputed on retry.
The buffer accepts durable over-limit source units, subject to spool capacity;
closing rejects payloads above 64 MiB and manifests above the existing 64 KiB
capture endpoint bound. The collector must split before closing; no oversized
unit is silently dropped or sent.

Capacity includes buffers, pending requests and quarantine: defaults are 10,000
items, 2 GiB, and a 5 GiB disk-free floor. All three are positive TOML integers.
Every buffer/cursor update and close checks capacity inside its transaction.
`CapacityError` rolls back the cursor and emits a fixed operator notification
through an injectable callback (warning logger by default). The collector must
stop fetching when it receives this error. No age-based automatic deletion.
Private spool bookkeeping/Receipt tombstones remain for deduplication; SQLite
reuses freed payload pages, and the disk-free floor also protects physical usage.

`deliver(httpx.Client)` performs one replay pass. Supply a base URL ending in
`/mobile/v1/`, private-file bearer credentials, verified TLS for tailnet, and
`trust_env=False`. The operator's `shepherd outbox INSTANCE --action flush
--token-file ...` uses the configured loopback listener only. Delivery queries
Receipt first, stages the identical payload, and PUTs the identical Manifest.
Timeout/5xx preserves bytes for the next pass; callers schedule retry/backoff.
No redirects are followed. Receipt ID and sha must match before durable Receipt
commit, followed by separate recoverable payload GC. Invalid Receipts preserve
bytes in quarantine. 401/403 pauses the entire instance durably. After repairing
registration, `--action resume` allows Receipt-first recovery with the new token.

Permanent 409/422 (also 413) preserves original bytes, Manifest, ID and error
code in private quarantine; retryable `409 payload_missing` remains pending.
Inspect with `shepherd outbox INSTANCE` (IDs/states/codes only). After diagnosis:

- `--action retry --capture-id ID` replays the exact immutable request.
- Create a new revision/capture through the collector when content needs repair.
- `--action discard --capture-id ID` explicitly deletes that local copy and keeps
  an ID tombstone; it never deletes the user's source or unblocks corrections.

This library does not start Webex polling, set account request budgets, define
correction-review grouping, or remove the rollback file Courier. The parallel
Archive Receipt/Loom/progress projection change owns those server projections.
