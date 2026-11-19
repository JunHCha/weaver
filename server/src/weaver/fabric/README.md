# Fabric

Fabric is an append-only log of `Thread` rows in SQLite
(`state_root/fabric.sqlite3`, schema version 2). Only facts a person approved,
or wrote by hand, enter it. The store assigns a monotonic `seq`; consumers keep
a `seq` bookmark and read `ThreadQuery(after_seq=...)`.

- `FabricStore.append(drafts)` is atomic and idempotent by `thread_id`: an
  identical replay returns the original rows, different content for an existing
  ID raises `ThreadConflict`. References (`resolves`, `blocked_by`, `retracts`,
  tag-correction `target`) must name existing rows, requests where required, and
  may not form a cycle. Triggers reject UPDATE, DELETE and replacement.
- `executes` names the Thread an Action executed and may not point at another
  `실행`, a tag correction or a rejection. A `실행` Thread records an Action's
  result (`actor=system`), not a business fact: `entries()` skips it unless the
  query sets `include_executions` or asks for `kind="실행"`, and `state()`,
  `contact_statistics()` and the wiki code ignore it too.
- `entries(query)` returns rows ordered by occurrence time. Tags are the
  effective tags: tag corrections are applied without changing the original row.
- `state.project(tag, threads)` computes the current `deadline`, `schedule` and
  `status` values (last value) and the assignee set (add/remove operations) of a
  tag from active threads, plus its unresolved requests. A retracted thread, a
  retraction itself, tag corrections and rejections contribute nothing.
- A database written by an older schema version is rejected with
  `SchemaVersionError`; data is not migrated.

`amends` is a human decision changing one request's deadline (ISO calendar date).
It names an existing request fact; schedules continue to use `updates` exclusively.
The amendment is history in read views. Effective deadlines fold active amendments
in occurrence/recording/seq order; a retraction restores the preceding value.
`FabricStore.edit(build)` validates and appends against one serialized snapshot.
A private `fabric_meta` epoch identifies this database generation for view bookmarks;
read-only opens of legacy v2 files do not migrate them. The next writable open
installs the epoch without changing immutable Thread payloads.
