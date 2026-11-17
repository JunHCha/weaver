# Wiki project lifecycle

`BatchCompiler` asks for `promotions` alongside the pages in its existing single
whole-corpus `wiki.compile_batch` request. Each proposal identifies a name, area,
goal, end state, whether it is a one/two-day task, and evidence Thread IDs. The
model judges semantic identity, goal and end state; deterministic validation
requires at least two distinct active facts, exactly the same resolved area on
each, and multiple sources or occurrence dates. A short task stays within an
operation page. A page with `role=project` alone does not constitute promotion.

After page publication and dictionary sync, `ProjectLifecycle` journals promotion
and correction payloads in the compiler SQLite database before any Fabric side
effect. It registers an unedited dictionary project and marks it `promoted`, then
appends `actor=system` tag-correction Threads with stable `wiki-project:` IDs.
The original fact rows never change; retries replay the prepared timestamps.
Later compilations can attach additional evidence to the same project. Own
corrections do not trigger an otherwise unnecessary compilation; the bookmark
still advances only through the original bounded input snapshot.

Human retraction of a promotion correction, explicit removal of the project tag,
and human dictionary changes durably suppress further automatic promotion of
that name and overlapping evidence, including a renamed proposal. Nothing
retracts or rewrites the human action. Human-renamed dictionary entries continue
to resolve their historical tags through aliases.

Lifecycle checks run on every compiler tick, including ticks without new facts.
A current status of `완료`, `completed` or `done`, or 30 days since the latest
active business record's `recorded_at`, archives the dictionary project. Recording
time is intentional: importing an older occurrence still counts as a new record;
promotion corrections, executions and retracted facts do not reset inactivity.
Archiving is sticky; it preserves historical tags and dictionary resolution but
excludes the project from extraction candidates. The web project grouping hides
archived lanes by default and offers a display toggle. Lifecycle flags live in
an additive dictionary table; read-only readers accept older v1/v2 dictionaries
without that table and default both flags to false.

Tests in `server/tests/wiki/test_promotion.py` compile synthetic inputs with a
fake LLM and temporary stores, covering promotion, append interruption/retry,
completion/inactivity archives and durable human overrides.
