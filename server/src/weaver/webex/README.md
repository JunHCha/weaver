# Webex Integration OAuth

This module implements the OAuth/configuration part of the pilot, not a poller.
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
With no section, Webex is disabled and has no secret path. Enabling settings
does not start collection in this change.

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

A future poller calls `OAuth.access_token()` before each operation. It re-reads
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
