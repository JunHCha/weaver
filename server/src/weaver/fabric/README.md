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
- `entries(query)` returns rows ordered by occurrence time. Tags are the
  effective tags: tag corrections are applied without changing the original row.
- `state.project(tag, threads)` computes the current `deadline`, `schedule` and
  `status` values (last value) and the assignee set (add/remove operations) of a
  tag from active threads, plus its unresolved requests. A retracted thread, a
  retraction itself, tag corrections and rejections contribute nothing.
- A database written by an older schema version is rejected with
  `SchemaVersionError`; data is not migrated.
