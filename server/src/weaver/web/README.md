# Local web edits

The Typer `web` command binds only to loopback and retains TrustedHost validation
and umask 077. GET endpoints open existing state read-only; neither session nor
dictionary reads initialize missing databases. SQLite WAL sidecars are permitted.

Fetch `GET /api/session` (non-cacheable JSON `{token}`) before writing. Every write
requires `X-Weaver-Token` and a same-origin loopback `Origin`, falling back to
`Referer` only when Origin is absent. Next's development rewrite supplies the
original loopback authority in `X-Forwarded-Host`; remote authorities are rejected
and the token is still required. There is no cross-origin access allowance.

- `POST /api/threads/{id}/tags`: `{add: string[], remove: string[], kinds?:
  Record<string, "project" | "person" | "organization" | "topic">}`. Unknown
  additions require a `kinds[name]`; aliases resolve to canonical names. The
  response is the single appended human 태그정정 Thread, using its target's source.
  Removing a displayed canonical name removes all matching historical spellings.
- `GET /api/dictionary`: name-sorted entries with `name`, `kind`, `aliases`,
  `human_edited` and `usage_count`. Usage counts active business Threads with
  effective tags, deduplicating aliases and excluding retraction pairs.
- `PATCH /api/dictionary/{name}`: `{name?: string, kind?: kind}`.
- `POST /api/dictionary/{name}/aliases`: `{alias: string}`.
- `DELETE /api/dictionary/{name}/aliases`: `{alias: string}` JSON body.
- `POST /api/dictionary/{name}/merge`: `{target: string}`; retains the target kind.

Dictionary writes use the public transactional ContextDictionary API. Fabric
writes use its append-only transaction; no resident-daemon lock is taken.
Dictionary registration precedes append, so a failed append may leave a valid,
unused human dictionary entry; retry reuses it. Database lock errors return 503
with `Retry-After: 1`; dictionary validation errors return 422. Thread lookup
errors return 404 and missing token or invalid origin returns 403.

Thread and state reads resolve effective tags through one dictionary snapshot,
so renames and merges need no historical log rewrite. The legacy tags endpoint
also retains historical labels for existing consumers.
