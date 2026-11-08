# Spinner

`Spinner.spin(source, reprocess=False)` is the one-shot Source → Thread API.
It reads payloads only through `SourceLocator.locate`, checks the archived Source
and SHA-256, and decodes UTF-8 without newline conversion.

- `text/kakao`: existing `parse_kakao` adapter, no revision.
- `text/share_text`: original complete text and one Segment, no revision.
- `text/call`: uses the built-in `GalaxyCallParser` for Galaxy scripts (#56), then an
  injected `Reviser` (production: `LlmReviser(client)`). The manifest's
  `hints.counterpart` becomes `ReviseHints.counterpart`. Only speech Segments
  (speaker present, nonblank text) are sent to the LLM; headers, notice, title,
  blank/unknown lines and the separators between Segments pass through verbatim.
  Revision wire format 2 (`spinner.revise.v2`) allows `start` with a null `end`
  like Segment; `revision_to_thread` restores original separators instead of
  joining with `\n` and rejects changed structural Segments.
- `fleece` and unused channels fail explicitly. STT is deferred by ADR-028.
  `TextSpinner` likewise refuses call scripts rather than treating them as prose.

```python
spinner = Spinner.from_config(
    archive,
    settings,
    configuration_version="galaxy-parser1:model-id:spinner.revise.v2",
    reviser=LlmReviser(client),
)
thread = spinner.spin(source)
```

`TextParser.parse(source, text) -> Thread` is the replacement boundary for each
channel; `parsers={"call": custom_parser}` overrides the built-in Galaxy parser.
No live LLM client is constructed implicitly. Callers
supply `configuration_version` for engine settings not advertised by adapters.
The stored key appends a SHA-256 fingerprint of that caller version, timezone key,
built-in `PARSER_VERSION`, revision `PROMPT_VERSION` and `FORMAT_VERSION`, and the
`(channel, version)` of every installed parser plus the reviser `version` (absent
means unversioned). Installing the default `call` parser therefore changed the key
for every channel, including `kakao` and `share_text`, once: their existing
checkpoints are recomputed on first use (intended; results are deterministic and
no LLM is involved for those channels). `LlmReviser.version` includes the
prompt content and production model identifier; custom clients can advertise their
own `version`. Bump the built-in parser constant when parsing rules change and an
adapter's version when its behavior changes. Unversioned adapters remain supported
and require a caller version bump. Versions retain independent results in `state_root/spinner.sqlite3`; a Source identity mismatch is an error.

SQLite stores original Source metadata, parsed Thread checkpoints, complete
Threads and stage attempts. Completed results become visible in one SQLite
commit with the successful revision attempt. A crash after that commit reuses the
result without another LLM request; a crash before commit leaves neither success
nor a complete result. A response lost before commit can still require another
request. Parsing commits with its successful attempt before revision, so failed
revisions and process restarts reuse that checkpoint. `reprocess=True` clears only the current version's parsed
and complete values before starting; attempt history and other versions remain.
A separate `spinner.sqlite3.lock` flock serializes the entire operation per DB,
including retries, across instances/processes. Files are private (0600), commits
use WAL, FULL synchronous and fullfsync, matching ArchiveStore and FabricStore.
WAL lets readers inspect attempts while a stage commits and preserves the existing
SQLite durability settings; flock still serializes writers. A missing state root
is created with mode 0700. Archived payloads are opened with `O_NOFOLLOW`.

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
Control-flow exceptions such as KeyboardInterrupt/SystemExit are interrupted,
not permanent failures. Orphaned running entries are marked on startup and again
immediately after each spin acquires flock, including on pre-existing instances;
elapsed time after a process death is unknown (`NULL`). Adapter ValidationErrors
are replaced by the content-free `SpinnerError("invalid_adapter_thread")`.
Logs carry identifiers, stage, status and timings, never payloads, counterpart hints or exception messages.
No scheduler, Courier or launchd integration is included.
