# Context dictionary

`ContextDictionary(state_root)` upgrades `context.sqlite3` v1 to v2 in one
transaction and applies mode 0600 with `fchmod` after successful initialization.
The wiki, wiki_alias, learned and corrections tables retain their v1 rows; v2
adds edit payloads. An interrupted upgrade rolls back its DDL and version change,
and concurrent openers serialize at `BEGIN IMMEDIATE`. Reopening is idempotent.
`open_readonly(path)` reads v1 and v2 without migration, chmod or writes.
**Older v1 daemons cannot read v2: this upgrade has no rollback.**

Public API for CLI and web callers:

- `snapshot()` reads the dictionary once in a transaction and returns a
  `DictionarySnapshot` for repeated in-memory `resolve(name)` and `candidates`.
  Canonical names take precedence over aliases, compared in NFC with Unicode
  casefold with all Unicode whitespace removed; canonical and alias collisions
  resolve in stable canonical name order (canonical names always win). A new wiki
  page whose canonical name equals an existing alias remains a distinct entry.
- `list_entries(limit=None)` returns stable name-sorted `ContextEntry` values.
  `resolve(name)` returns an entry or `None`; `kind_of(name)` uses that resolution.
  Speech misrecognitions are transcript candidates, not tag aliases.
- `add_entry(name, kind, aliases=(), human_edited=True,
  drop_conflicting_aliases=False)` returns the entry. An already resolved name
  is a no-op, including on retry. Automatic creation uses `human_edited=False`;
  a wiki page with the same name then wins for kind, summary and aliases, just
  as it does over a learned entry. The automatic fallback survives wiki deletion.
  Approval uses `drop_conflicting_aliases=True` to discard only aliases that
  became occupied after preparation; it logs a content-free warning and still
  creates the entry, so Fabric append can proceed. A now-occupied canonical name
  returns the existing entry instead.
- `rename(name, new_name, human_edited=True)` retains the old name as an alias.
  The automatic `human_edited=False` path accepts only unedited learned entries,
  keeps that flag false and stores the former spelling only as a transcript
  correction (never a tag alias). `set_kind(name, kind)`,
  `add_alias(name, alias, human_edited=True)`, `remove_alias(name, alias)` and `merge(source, target)`
  return the updated entry and mark it `human_edited`. Every edit reads and writes
  within one `BEGIN IMMEDIATE` transaction. Merge retains the target kind,
  combined aliases and learned corrections.
- Human edits store only name/kind overrides and added/removed alias sets.
  Summaries and unedited aliases follow the current wiki snapshot; a rename alone
  also follows wiki kind changes. Removing an alias for a renamed/merged source
  reveals its wiki page again. Wiki-origin edits are deleted when all their source
  wiki pages disappear from the snapshot; renamed learned contributions and
  corrections remain as learned entries. Standalone entries remain until an
  explicit future deletion API is provided. No persistent hidden-name tombstone
  can suppress a later wiki page.
- Unknown edit targets, blank labels, unsupported kinds and new conflicting names
  or aliases raise `ValueError` unless automatic conflict filtering was requested.

`sync_from_pages` replaces the wiki snapshot; `learn` retains latest
observed-spelling ownership. Renames/merges do not rewrite historical Fabric
tags, so follow-up tools should resolve those tags through the dictionary.

Automatic alias insertion uses `add_alias(name, alias, human_edited=False)`.
It preserves the existing human-edit flag, follows subsequent wiki kind/summary
changes, and cannot restore an alias a human explicitly removed. Collisions
still raise ValueError. Alias-only automatic payloads use an optional
`automatic_aliases` field in v2 edited rows; readers must use this updated
implementation after applying such aliases. Existing v1/v2 payloads without
that field remain readable. Normal add_entry retry semantics are unchanged;
callers must explicitly add aliases to an already existing canonical entry.

Kinds follow PARA: `project` is a curated project, `area` a broad ongoing
responsibility, and `topic` a resource; `person` and `organization` identify
parties. Existing SQLite tables store kind labels without a closed SQL enum,
so `area` needs no data rewrite or database version change. Wiki page roles map
`project → project`, `operation → area`, `reference → topic`, and person and
organization unchanged. `page_role` takes precedence over a legacy `kind` field
when synchronizing pages. Web dictionary edits and `/api/tags` accept `area`.

A Wiki-synced project with `human_edited=False` and `promoted=False` remains resolvable for history
and dictionary management, but is excluded from approval/Wiki extraction tag
candidates, including recent-tag hints. Existing curated projects and existing
learned projects may be tagged when clearly relevant. Automatic approval and
bootstrap never create a project. Wiki batch promotion registers projects with
`human_edited=False` and `promoted=True`, making active projects eligible.
`set_project_lifecycle(name, promoted=None, archived=..., reverted=None)` stores automatic flags
separately from human overrides, without changing their provenance. Flags survive
Wiki sync and follow human renames/merges through historical aliases. Archived
projects remain resolvable but are excluded from all extraction tag hints and
validation. Human-reverted promotions set `promotion_reverted=True` and
`promoted=False`, which excludes even learned or human-edited entries from tag
candidates. The additive lifecycle table accepts earlier rows without `reverted`;
readers default it to false and writers add the column transactionally. Older
v1/v2 read-only dictionaries without the additive lifecycle table return false flags; writers create the table transactionally. See
[`wiki/README.md`](../wiki/README.md) for promotion and suppression journaling.
