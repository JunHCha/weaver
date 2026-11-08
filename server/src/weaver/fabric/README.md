# Fabric current state

`FabricStore.state(entity_id) -> weaver.schema.EntityState` replays one entity's
immutable facts. It is a read API and grants no execution permission. These
server-only models are excluded from Courier's `SHARED_MODELS`; wire schemas and
Android models are unchanged.

## Fold rules (issue #6, approved option A)

First evaluate the supersedes/retracts DAG from referring entries to their
targets, visiting referrers first. Only active entries suppress targets; an
inactive replacement does not suppress its predecessor. Then fold active,
non-retraction entries in UTC `(occurred_at, recorded_at, entry_id)` order.
Retraction entries contribute no value, even if they carry kind=결정 or resolves.
Storage rejects retraction of a retraction and validates same-field/entity
references. Timestamps must be known and timezone-aware, as in issue #5.

The following table uses entries A, B, C in increasing total order unless stated.
All original entries remain accessible through `entries`, including inactive ones.

| Input | Expected projection |
| --- | --- |
| A: deadline=`"Monday"`; B: deadline=`"Tuesday"` | `fields.deadline` is Tuesday, evidence B |
| A: date/due_at/status/assignee/custom=`"old"`; B: same field=`null` | Current value is null, evidence B retained; missing key means no active value ever contributed |
| A: assignees=`{add:["a","b"],remove:[]}`; B: `{add:["c"],remove:["a"]}` | Members b (A), c (B); latest operation B |
| A: assignees add 1 and true; B: add 1 again | Two members; number evidence B, boolean evidence A |
| A: assignees add `{x:[false]}` and `{x:[0]}` | Two distinct members; recursive JSON types matter |
| A: assignees add a; B: assignees=`null` | Empty set, clear evidence B |
| A: assignees=`{add:"a",remove:[]}` | `InvalidSetOperation(entry_id=A, field="assignees")` on state read |
| A and B: field=null, identical content | Both accumulated by distinct entry_id |
| A and B: kind=결정 or 논의, even with a non-null field | Both accumulated; kind takes precedence over field rules |
| B supersedes A | A suppressed; B contributes |
| B supersedes A; C retracts B | B suppressed; A restored; C has no value contribution |
| B supersedes A; C supersedes B | Active A and C; B is inactive and cannot suppress A; latest active value wins |
| B supersedes A; C supersedes A; D retracts C | B still suppresses A |
| B retracts A; C retracts B | Append rejects `retraction_of_retraction` |
| A: request; B and C: human decisions directly resolving A | No open A; representative C, both decision entries retained in accumulated history |
| Previous row; D retracts C | Representative B; retract B too and A reopens |
| A: request; B: decision resolves A; C: request supersedes A | C remains open; B does not resolve C |
| A: request; B: AI-approved decision without resolves | A remains open |
| No matching entity records | EntityState with empty fields, accumulated, sets, open_requests, decisions |

Only `assignees` uses a set; all other non-null field names use latest-value
semantics, including unknown names. Each set operation must have exactly `add`
and `remove` arrays (empty arrays are allowed). Add runs before remove, so remove
wins an overlap within an operation. Removing a missing member is a no-op.
JSON objects compare without key order, arrays with element order, and numbers
compare numerically (1 equals 1.0); booleans differ from numbers. Repeated adds
replace member evidence in place; new members append in add-array order. Null
clears the whole set. Malformed active operations fail explicitly on state read;
append preserves the original fact without interpreting field-specific values.

`fields` values and `sets` members contain `value`, `entry_id`, `source_id`.
Each `SetState` also records the last operation's `entry_id` and `source_id`,
including clears/removals. `accumulated`, `open_requests`, and `decisions` retain
full Entry models, with evidence, timestamps, kind and request content.
`decisions` maps directly resolved request IDs to latest active human decisions,
including decisions on now-inactive requests. An open request is an active
kind=요청 Entry without such a direct active decision. `blocked_by_request` does
not alter state or grant permission.

There is no state cache. Each call reads one entity snapshot in one SQLite SELECT
and returns fresh models; requery, reconnection, process restart, and replay of
the same fact set yield the same projection. Tests:

```sh
uv run --project server pytest server/tests/fabric/test_state.py
```
