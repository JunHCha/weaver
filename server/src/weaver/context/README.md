# Context dictionary

`ContextDictionary(state_root)` upgrades `context.sqlite3` v1 to v2 in place,
inside a transaction. The wiki, wiki_alias, learned and corrections tables are
retained; v2 adds edited entry payloads and hidden source names. Reopening is
idempotent. `open_readonly(path)` can read v1 and v2 without migration or writes.

Public editing API for CLI and web callers:

- `list_entries(limit=None)` returns stable name-sorted `ContextEntry` values.
- `resolve(name)` returns an entry or `None`; canonical names precede aliases,
  compared in NFC with Unicode casefold. Speech misrecognitions are candidates,
  not tag aliases. `kind_of(name)` uses the same resolution.
- `add_entry(name, kind, aliases=(), human_edited=True)` returns the entry;
  an already resolved name is a no-op, including on retry. Automatic creation
  must pass `human_edited=False`.
- `rename(name, new_name)` keeps the old name as an alias. `set_kind(name, kind)`,
  `add_alias(name, alias)`, `remove_alias(name, alias)` and
  `merge(source, target)` return the updated entry and mark it `human_edited`.
  Merge retains the target kind, both sets of aliases and learned corrections.
- Unknown edit targets, empty labels, unsupported kinds and conflicting names
  or aliases raise `ValueError`. Existing ambiguous wiki aliases resolve in
  stable name order. Edits are atomic; wiki snapshots cannot overwrite them.

`sync_from_pages` still replaces the wiki snapshot, and `learn` retains latest
observed-spelling ownership. `candidates` keeps fuzzy transcript matching.
`ContextEntry.human_edited` identifies explicit edits; wiki and automatic entries
start false. Editing a wiki entry preserves its summary and old name without
renaming the wiki file. Renames/merges do not rewrite historical Fabric tags;
follow-up tools should resolve those tags through the dictionary.
