# Editable outputs

`Publisher` writes its AI folder and records `note_id`, vault-relative `path`,
content `sha256`, and `status` in `state_root/publish.sqlite3`. Users copy those
notes elsewhere in the same vault. `SqliteOutputWatcher` reads only frontmatter
outside the configured AI folder and the manifest's managed paths (including
stalled paths from an older folder setting). It never changes a vault file.
Hidden/temporary files, symlinks, and notes without source IDs are excluded.

The shared editable properties have the same names as Fabric Entry fields:

| Property | Value | Entry value |
| --- | --- | --- |
| `deadline` | ISO calendar date `YYYY-MM-DD` | normalized string |
| `assignees` | array of `ent_…` IDs | `{add: [...], remove: [...]}` |
| `status` | nonempty text | whitespace-normalized string |
| `decision` | nonempty text | whitespace-normalized string |

Removing a property or setting YAML null emits `value=null`. `decision` edits
are `kind=변경` facts; they do not resolve requests or authorize execution.
Publisher emits valid editable state properties on object notes, and scalar
editable properties on record notes. Record `assignees` operations are not
exported as a snapshot; use an object note for editing the whole assignee set.
Metadata such as `weaver_entry_status` remains descriptive and is not editable
fact input. Markdown prose is never interpreted. No language model is involved.

The supported flat frontmatter dialect includes JSON/YAML quoted or plain
scalars, inline JSON arrays, and YAML block lists, with or without indentation.
Inline comments preserve hashes inside quoted strings. Duplicate keys, nesting,
aliases, tags and multiline values reject the whole file, including unsupported
structures in unknown properties. Unknown flat properties are validated but are
not used as facts. Frontmatter is limited to 64 KiB. An invalid field,
unknown schema, or incomplete source/entity/entry metadata rejects that file's
whole snapshot, logs only an error type, and preserves its previous baseline.

```python
watcher = SqliteOutputWatcher(
    AiFolder(config.vault_root, config.publish.folder),
    config.state_root / "output-watcher.sqlite3",
    timezone=config.time.zone,
    manifest=PublishState(config.state_root / "publish.sqlite3"),
)
entries = watcher.scan()
fabric.append(entries)  # may safely repeat after a crash
watcher.acknowledge([entry.entry_id for entry in entries])
```

`OutputWatcher` keeps the `scan() -> list[Entry]` Protocol. The concrete
`acknowledge` method belongs to the delivery boundary, never to the scanner's
Fabric side effects. `OutputSync.run()` implements this sequence; enabled
`[publish]` runtime wiring runs it before ordinary AI publication. Human output
edits are already human facts, so no extra proposal approval is requested
(ADR-011). This recording has no calendar adapter or execution permission.

The private SQLite database (0600, FULL/fullfsync) stores stable output IDs,
normalized successful baselines, pending target snapshots and immutable Entry
payloads. A transaction serializes scans and commits the pending payloads and
local scan date together. Failed append or a crash before acknowledgment returns
the identical IDs, values and timestamps again, including after restart. Fabric's
identical append is idempotent. A partially acknowledged batch advances its
baseline only after all its entries have been confirmed. Confirmed payloads are
retained to make repeated acknowledgments safe. Do not acknowledge before append.
The state database must be outside the vault; production uses `state_root`,
which configuration also keeps outside sync roots.

Event IDs are deterministic hashes encoded as valid `evt_` Crockford IDs from
(output ID, field, previous value, new value, baseline revision). The revision
identifies the previous successful comparison: it stays fixed on retries but
separates later repetitions such as A→B→A→B. Without that discriminator a later
real edit could collide with an already appended event. Occurrence time is the
scan time, not a claim about when an editor saved the file. Supersedes/retracts
are null: the change is a new fact, not a rewrite of the original record.

First discovery only creates a baseline. Registration keys combine note ID and
path, so copies get independent `output:<ID>` sources. If exactly one previously
active path disappears and exactly one new path has the same note ID, registration
continues at the new path. Multiple possible old or new paths are ambiguous and
get fresh baselines instead of guessing. Deletion marks a registration inactive
without a deletion Entry; already pending entries still deliver. Divergent copies
are recorded independently without resolving conflicts.

Comparison happens once per configured local date, at the next runtime tick.
Same-day calls only replay pending entries. Missed dates after sleep/restart cause
one comparison, without inventing intermediate edits. Pending outputs freeze
their target until confirmation; subsequent edits compare on the next daily pass.
Invalid files retry on the next day, without advancing their baseline. File mtime
is not used as evidence (even edits preserving mtime are detected). This is a
single-PC local-filesystem boundary, not an editor audit trail.
