# Spinner

`Spinner.spin(source, reprocess=False)` is the one-shot Source → Thread API.
It reads payloads only through `SourceLocator.locate`, checks the archived Source
and SHA-256, and decodes UTF-8 without newline conversion.

- `text/kakao`: existing `parse_kakao` adapter, no revision.
- `text/share_text`: original complete text and one Segment, no revision.
- `text/call`: requires an injected `TextParser` for Galaxy scripts (#56), then an
  injected `Reviser` (production: `LlmReviser(client)`). The manifest's
  `hints.counterpart` becomes `ReviseHints.counterpart`.
- `fleece` and unused channels fail explicitly. STT is deferred by ADR-028.
  `TextSpinner` likewise refuses call scripts rather than treating them as prose.

```python
spinner = Spinner.from_config(
    archive,
    settings,
    configuration_version="galaxy-parser1:model-id:spinner.revise.v1",
    parsers={"call": galaxy_parser},
    reviser=LlmReviser(client),
)
thread = spinner.spin(source)
```

`TextParser.parse(source, text) -> Thread` is the replacement boundary for each
channel. No Galaxy parser or live LLM client is constructed implicitly. Callers
must change `configuration_version` when engines, parser rules, models, prompts,
timezone or other output-affecting settings change. Versions retain independent
results in `state_root/spinner.sqlite3`; a Source identity mismatch is an error.

SQLite stores original Source metadata, parsed Thread checkpoints, complete
Threads and stage attempts. Completed results become visible in one SQLite
commit. Parsing commits before revision, so failed revisions and process restarts
reuse that checkpoint. `reprocess=True` clears only the current version's parsed
and complete values before starting; attempt history and other versions remain.
A separate `spinner.sqlite3.lock` flock serializes the entire operation per DB,
including retries, across instances/processes. Files are private (0600), commits
use FULL synchronous and fullfsync. The configured state root must already exist.

`[spinner]` has `max_attempts=3`, `backoff_seconds=0.25`, and
`max_backoff_seconds=4`. Only `SpinnerTransientError` requests bounded exponential
retries. All `LlmError` exceptions propagate immediately: LlmClient owns its own
retry policy. Invalid inputs, adapter results and other exceptions fail once.
No synchronous parser timeout is forced. A subsequent `spin` starts a new bounded
retry sequence and reuses a successful parsing checkpoint.

The `attempts` table records `parse`, `revise`, per-attempt `total`, and the entire
`spin` duration (including backoff, excluding flock wait) using an injectable
monotonic clock. `spin` uses attempt zero; stage attempts use one-based numbers.
Durations are nested observations, not additive LLM cost or latency records.
Interrupted running entries are marked on startup under the same lock; elapsed
time after a process death is unknown (`NULL`). Logs carry identifiers, stage,
status and timings, never payloads, counterpart hints or exception messages.
No scheduler, Courier or launchd integration is included.
