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
  casefold; colliding aliases resolve in stable canonical name order. A new wiki
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
- `rename(name, new_name)` retains the old name as an alias. `set_kind(name, kind)`,
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
