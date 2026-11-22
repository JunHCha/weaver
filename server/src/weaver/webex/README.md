# Webex Integration OAuth and polling

This module implements pilot stages 5 and 6 (OAuth/configuration and polling).
`[shepherds.webex]` exposes collection settings, including the immutable
`exclude_space_ids` tuple. `[privacy.channels.webex]` and the LLM gate belong to
the separate policy change; exclusion and gate wiring happen when the changes
are integrated. No schema or Android contract changes are needed here.

Design references (local, not committed):

- `docs/domain/architecture/webex-shepherd-pilot.md:22-26` (scopes, state, secrets),
  `:54-76` (TOML and exclusion semantics), `:95` (stage 5).
- `docs/domain/decisions/047-webex-shepherd-pilot.md:20` (ADR-023 exception).
- `docs/domain/architecture/multi-shepherd-capture.md:65` (OAuth and Courier
  registration remain separate).

## Setup

Register a Webex Integration with only `spark:rooms_read`,
`spark:messages_read`, and `spark:kms`. Register an exact loopback redirect URI,
for example `http://127.0.0.1:45123/oauth/callback`. The port must be unprivileged
and available. The CLI binds only that address, before opening the browser,
for at most 180 seconds plus an in-flight bounded HTTP request. Callback
connections time out after five seconds, and callback query strings are never
logged. This is a local browser flow, with no public webhook or listener.

Add paths, not secret values, to your existing server TOML:

```toml
[shepherds.webex]
enabled = true
instance_id = "mac-webex-primary"
auth = "integration_oauth"
credentials_file = "private/webex-oauth.json"
courier_token_file = "private/webex-capture.token"
exclude_space_ids = []
poll_interval_seconds = 300
initial_backfill_days = 7
reconcile_overlap_days = 7
bundle = "space_day"
timezone = "Asia/Seoul"
attachments = "metadata_only"
```

Relative paths use the TOML parent. `~/` uses HOME; environment substitution
is unsupported. Secret parents must be outside sync_root, vault_root and any
separate wiki vault. Symlink paths and identical credential/Courier files are
rejected. Config loading does not read secrets or create their directory.
With no section, Webex is disabled and has no secret path. Enabling settings does not start a worker; run `webex poll` explicitly.

```sh
uv run --project server python -m weaver.app webex configure --config /path/to/config.toml
uv run --project server python -m weaver.app webex consent --config /path/to/config.toml
uv run --project server python -m weaver.app webex status --config /path/to/config.toml
uv run --project server python -m weaver.app webex revoke --config /path/to/config.toml
```

Configure prompts for client ID and secret with hidden input. Existing
registration is never overwritten. If needed, remove the revoked credential
file yourself before configuring a different Integration. The callback URI
must match the URI registered with Webex; Weaver validates the local URI shape
and uses it unchanged for consent and exchange. `consent --no-browser` prints
the authorization URL for manual opening; it contains state, not tokens.
Treat that URL as private. New Spaces are automatically included unless their
IDs are excluded. The collection implementation must enforce exclusions
before message/attachment reads and stop pending work for newly excluded IDs;
these settings alone do not perform collection or delete historical data.

## Private storage and token use

The credential JSON contains client registration, access/refresh tokens,
response-derived expiry timestamps and a single-use pending state. Its file
and lock are owner-only 0600; its parent is owner-only 0700. Missing directories
are created 0700, while existing unsafe permissions, symlinks and hard-linked
files are rejected. The parent is pinned by a directory descriptor, writes use
an fsynced 0600 temporary file followed by atomic replace and directory fsync,
and a process lock serializes consent, rotation, and revoke. Do not put this
private directory in backups, cloud sync, logs, prompts, or a vault.

The poller calls `OAuth.access_token()` before each operation. It re-reads
durable state rather than caching a token, refreshes once when expired, and
atomically stores replacement tokens and response expiry. If refresh returns
no replacement refresh token, its existing expiry is retained. Missing/expired
refresh tokens and token-endpoint 400/401/403 require reauthentication. Other
HTTP/network errors surface fixed error codes with no immediate retry loop,
provider body, or token output. On a resource 401, the poller may request one
`force_refresh=True`; a second 401 must stop collection and require consent.
No response status advances a collection cursor here.

`revoke` removes access/refresh tokens and pending state through atomic
replacement, leaving client registration in a reauthentication-required state.
Existing OAuth objects and newly opened objects then refuse token access.
It retains instance_id, Courier credentials, queued work and source journals.
File replacement removes logical references; it cannot promise physical
secure erasure on SSDs, snapshots or backups.

Remote grant revocation is a separate user action: open **Webex Authorized
Apps** and remove the Integration. The coordinator approved this choice on
2026-11-20: do not add administrator `identity:tokens_read/write` scopes just
to call the admin `/authorizations` revocation API. Local revoke alone does
not invalidate a token already copied elsewhere or sent in a request.

Protocol sources: [Authentication](https://developer.webex.com/admin/docs/authentication),
[Integration setup](https://developer.webex.com/admin/docs/integrations),
[Admin authorizations](https://developer.webex.com/admin/docs/api/v1/authorizations).
Tests use fake credentials, fake HTTP responses and a fake callback server;
real account Integration permissions and API behavior remain deployment checks.


## Source polling (stage 6)

`webex poll --config ...` runs at startup and every `poll_interval_seconds`
(default 300); `--once` runs one budgeted round. The account worker takes a
nonblocking process lock beside its private B4 outbox. `webex poll-status`
reports only counts, usage and retry deadlines. No Space titles, IDs or message
bodies are logged. A 403/404 isolates that Space as `access_gap`, preserving its
cursor; it never establishes deletion. Other invalid pages stop with a fixed
operator error. After investigating access, stop the worker and run `webex poll-resume` to
retry isolated Spaces from their original saved cursors.

Optionally override the four `request_budget_*` integers in `[shepherds.webex]`: `total`,
`incremental`, `overlap`, and `deletion` (full names use that prefix). The
account ceiling and each task ceiling count Messaging API attempts in a fixed
`poll_interval_seconds` UTC window, including room discovery, retries and
401 resource retries. All caps must be positive and the task caps together must
fit the account cap. Defaults are total 300, incremental 200, overlap 80 and
deletion 20 per 300-second interval (60 total attempts per minute on average).
Polling starts with these defaults when budget settings are omitted. OAuth token exchanges retain the
bounded OAuth implementation and are separate from Messaging API accounting.
One configured worker owns one account; do not configure concurrent instances
with the same credentials. These are local safety ceilings, not Webex quotas.

Discovery rechecks exclusions before any message request, including new Spaces.
Room pagination yields to incremental work; scheduling alternates priority in
very small windows to prevent discovery or message collection from starving.
Incremental scans descend from a fixed upper timestamp to the previous
created-time/message-ID high-water, including timestamp ties. Only the first
scan starts at seven days; catch-up after longer downtime reaches the saved
high-water. Overlap has its own seven-day floor, pagination, completion time and
round-robin Space position, and uses only its own cap plus remaining account
budget. It never changes the incremental high-water. A response page's observed
payloads, hashes, scan position and completed high-water commit together via
`Outbox.buffer_page`; a failed commit or full spool advances none of them.
All source state uses `webex.poll.v1`, independently of Courier `/changes`.

Empty pages with a Link `next` continue. Links must remain HTTPS on the exact
Webex API origin and endpoint; message links must keep the same room ID.
Cycles and unexpected ordering/ranges stop rather than silently skipping data.
Metadata contains attachment URLs already present in message JSON; attachment
bytes, file metadata endpoints, OCR and People lookup are never requested.
Message author display is optional; private email/ID is not substituted.
429 honors numeric or HTTP-date Retry-After with jitter, and transport/5xx
failures back off. Retry deadlines and attempt usage survive reboot. A resource
401 refreshes once, including when budget exhaustion defers that retry.

Missing overlap IDs become persistent unverified candidates. Single-message
checks run only following that Space's successful overlap in the same run and
within both budgets. A 403/404 remains unverified because retention or access
loss is ambiguous. No automatic deletion or completeness claim is made.

This stage writes B4 **open buffers**, retaining earlier observed message
versions in distinct revisions. Stage 7 owns next-day closure, deterministic
64 MiB parts, immutable Manifest creation, correction links, Receipt-dependent
publication and review integration. `poll` does not close bundles or send
unfinished current-day data to the Archive. Existing B4 flush remains unchanged;
there is no second active Courier path. Before enabling publication, deploy the
Webex-capable APK first (the existing enum compatibility requirement).

REST fixtures explicitly assume descending `created` order, `before` filtering,
Link continuation and inclusion of replies. These are synthetic assumptions,
not observed account behavior. Exact REST filters/reply coverage, retention and
account Integration permission remain manual pilot acceptance TODOs; no actual
Webex API, LLM or user vault is used in tests.

Design basis: `docs/domain/architecture/webex-shepherd-pilot.md:30` (polling),
`:36` (source cursors), `:44` (best-effort deletion), `:84` (budget/backoff),
`docs/domain/architecture/multi-shepherd-capture.md` (B4 buffers), ADR-044/047.

## Dictionary lineage prerequisite

The pre-fix regression test reproduces a first-summary `policy_blocked` after
Webex enters an archive containing a dictionary. Dictionary changes now record
origin Source IDs and an observation timestamp for unknown content in private
SQLite. Summary approval and re-extraction supply their originating Source ID.
Unknown content observed strictly before the first Webex archive Receipt is
proven non-Webex; equal/later timestamps and missing legacy timestamps remain
closed. Migration records the current observation time, never a guessed
filesystem creation time. Shared Fabric origins cannot hide an unrelated
unknown dictionary entry. No public schema or Kotlin model changes are needed.

Policy basis: `docs/domain/decisions/046-central-llm-policy-gate.md` and the W3-B5
coordinator decision; implementations are `context/__init__.py:251`,
`context/__init__.py:470`, `llm/policy_archive.py:185`.
