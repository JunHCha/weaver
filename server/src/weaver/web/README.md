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
  Only additions/removals that change the effective tags are appended. Empty or
  repeated edits return 200 with the current target Thread and append nothing.
  Removing a displayed canonical name removes all matching historical spellings.
- `GET /api/dictionary`: name-sorted entries with `name`, `kind`, `aliases`,
  `human_edited`, `promoted`, `archived` and `usage_count`. Usage counts active business Threads with
  effective tags, deduplicating aliases and excluding retraction pairs.
- `PATCH /api/dictionary`: `{entry: string, name?: string, kind?: kind}`.
  Exactly one of name/kind is required; requests changing both are rejected with
  422 before writing. Unknown entries return 404 before opening a writable store.
- `POST /api/dictionary/aliases`: `{entry: string, alias: string}`.
- `DELETE /api/dictionary/aliases`: `{entry: string, alias: string}` JSON body.
- `POST /api/dictionary/merge`: `{entry: string, target: string}`; retains the target kind.

All entry names are JSON fields, including names containing `/`. Write bodies
are capped at 64 KiB before JSON parsing, including streamed bodies (413 when
exceeded). Add/remove lists and the kinds map each allow at most 100 elements;
names, aliases and merge targets allow 1–256 characters (422 when exceeded).

Dictionary writes use the public transactional ContextDictionary API. Fabric
writes use its append-only transaction. Existing v2 dictionary edits need no
resident-daemon lock. When the dictionary is v1, the web writer must acquire
`state_root/weaver.lock` and hold it through initialization/migration. If a daemon
holds that lock, writes refuse the upgrade with 503 and a message asking to stop
the daemon first; GETs and no-op edits remain read-only. This prevents upgrading
state behind a running v1 daemon. The ContextDictionary public API is unchanged.
Dictionary registration precedes append, so a failed append may leave a valid,
unused human dictionary entry; retry reuses it. Database lock errors return 503
with `Retry-After: 1`; dictionary edit ValueErrors return fixed 422 messages,
never exception text. That translation applies only to write handlers in the edit
router; unrelated failures remain 500 and SQLite/schema failures remain 503.
Thread lookup errors return 404 and missing token or invalid origin returns 403.

Thread and state reads resolve effective tags through one dictionary snapshot,
so renames and merges need no historical log rewrite. The legacy tags endpoint
also retains historical labels for existing consumers.

Dictionary kinds include PARA `area` (ongoing responsibility) and `topic`
(resource). `/api/tags` and dictionary edit requests support `area`.
`GET /api/threads` adds a derived read-only `presentation: fact | history`.
History covers tag corrections (태그정정), executions (실행), rejections (거절),
retraction records (`retracts` set), new Wiki field histories (`wiki-field:` ID),
and legacy Wiki field histories (`wiki:` ID, `field` set, and content exactly
`<field> 변경`). All other Threads are facts, including resolved requests.
A retracted business original retains `fact`; the effective view hides that
original using its retraction record instead of changing its presentation.
This attribute is never stored in Fabric and adds no shared schema field.
Thread `counterpart` is a single canonical person/organization or null and also
appears in its tags; tag corrections do not change counterpart. The phone's
strict PipelineItem wire format deliberately omits counterpart entirely.

`GET /api/tags` includes `archived` on each tag. Archived projects remain
readable and editable in the dictionary and in historical Threads. Project
grouping hides their lanes by default; “보관 프로젝트 표시” reveals them.
Other groupings retain the underlying business facts.
