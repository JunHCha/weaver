# Weaver

Turn everyday phone records into reviewed personal knowledge.

Weaver is a personal tool being built to collect call recordings, voice memos,
KakaoTalk exports, and shared text. A local Mac server transcribes audio and
organizes the material, using an LLM for revision and extraction. Human review
comes before proposed facts enter a central timeline log and are exported to a
personal knowledge vault such as Obsidian.

**MVP in progress:** the foundation modules are implemented; the complete capture,
processing, review, and export workflow is still being assembled. Follow the
[MVP roadmap (#37)](https://github.com/JunHCha/weaver/issues/37).

## How it works

The names follow a weaving metaphor: **Shepherd** captures material on the phone,
**Courier** transports files through Syncthing, **Spinner** turns audio into text,
**Loom** prepares and applies reviewed proposals, and **Fabric** preserves the
resulting facts before vault export.

The diagram shows the intended workflow, including components still planned.
Courier's four folders carry files in the directions shown; they are transport
routes, not the raw archive or the knowledge vault.

```mermaid
flowchart TB
  subgraph phone["Phone · Android"]
    direction LR
    shepherd["Shepherd<br/>Capture audio, KakaoTalk, shared text"]
    review["Human review<br/>Approve · Edit · Revise · Reject"]
  end

  subgraph sync["Sync · Courier / Syncthing"]
    direction LR
    outbox["outbox/<br/>Captures + payloads"]
    receipts["receipts/<br/>Archive acknowledgements"]
    proposals["proposals/<br/>Review packages"]
    decisions["decisions/<br/>Verdicts"]
  end

  subgraph mac["Mac server · Local processing and storage"]
    direction TB
    archive["SourceStore<br/>Raw archive outside the vault"]
    spinner["Spinner<br/>Local STT + LLM revision → Thread"]
    loom["Loom<br/>Refine · Extract · Propose"]
    apply["Apply review<br/>Validate approved facts"]
    rejected["Rejected proposal<br/>No facts appended"]
    llm["LlmClient<br/>Single boundary to Anthropic Claude"]
    fabric[("Fabric<br/>Append-only SQLite timeline")]
  end

  subgraph outputs["Outputs · Planned"]
    export["Vault export<br/>Fold facts into current state"]
    vault["Personal knowledge vault<br/>Markdown notes and links"]
  end

  shepherd --> outbox --> archive
  archive --> receipts --> shepherd
  archive -- "Audio / Fleece" --> spinner
  archive -- "Text / KakaoTalk parsing" --> loom
  spinner -- "Thread" --> loom
  spinner -. "Revision" .-> llm
  loom -. "Extraction" .-> llm
  loom --> proposals --> review
  review --> decisions --> apply
  apply -- "Revise with instructions" --> loom
  apply -- "Approve / validated edits" --> fabric
  apply -- "Reject" --> rejected
  fabric --> export --> vault

  classDef capture fill:#dbeafe,stroke:#2563eb,color:#172554
  classDef transport fill:#fef3c7,stroke:#b45309,color:#451a03
  classDef process fill:#ede9fe,stroke:#7c3aed,color:#2e1065
  classDef human fill:#fce7f3,stroke:#be185d,color:#500724
  classDef stored fill:#dcfce7,stroke:#15803d,color:#052e16
  class shepherd capture
  class outbox,receipts,proposals,decisions transport
  class spinner,loom,apply,llm,rejected process
  class review human
  class archive,fabric,export,vault stored
```

STT runs locally; LLM revision and extraction use an external Anthropic API.
Approving a proposal records facts. It does not authorize execution of requests
found in the source material.

| Term | Meaning |
| --- | --- |
| Fleece | Raw audio, such as a call recording or voice memo. |
| Thread | A source's text, with segments and provenance retained. |
| Proposal | A reviewable bundle of proposed entities, relations, schedules, and fact drafts from one source. |
| Verdict | The review result: approve, edit, revise with instructions, or reject. |
| Entry | One recorded fact in Fabric, with time, source, actor, and entity references. |
| Fabric | The central append-only log; corrections add facts rather than overwrite history. |

See the [v1 JSON contracts](schema/README.md) for the wire formats and review rules.

## Current status

Implemented foundations:

- Server package, Typer CLI scaffold, configuration, and v1 schemas.
- Server Courier file handling and SourceStore raw archiving.
- Anthropic `LlmClient` and local MLX `Stt` adapter.
- SQLite Fabric storage for append-only Entries and queries.
- Android app scaffold, schema models, and a Storage Access Framework (SAF)
  Courier client with diagnostics.
- Release/manual CI for server and Android verification.

In progress or planned: capture automation, KakaoTalk parsing, Spinner revision
orchestration, refinement rules, extraction and proposal generation, the human
review interface and Verdict handling, state folding, and vault export.
The server CLI currently validates configuration and initializes directories;
its resident processing runtime is not implemented yet. The Android Courier
client is a foundation for the planned capture and review experience.

## Repository and stack

| Path | Contents |
| --- | --- |
| [server/](server/) | Python server modules and tests. |
| [android/](android/) | Android app and contract tests. |
| [schema/](schema/) | Generated v1 JSON Schemas and synthetic examples. |
| [.github/workflows/](.github/workflows/) | Manual and release verification. |

- **Server:** Python 3.12+, uv, Typer, SQLite, Ruff, and strict mypy.
- **Speech:** MLX Whisper `large-v3-turbo` on Apple Silicon; local model
  preparation and `ffmpeg` are required for real transcription. The MVP adapter
  does not identify speakers.
- **LLM:** Anthropic Claude through a shared `LlmClient` boundary.
- **Android:** Kotlin, Jetpack Compose, kotlinx.serialization; minSdk 26,
  compileSdk and targetSdk 36.
- **Transport:** Syncthing between devices, with Syncthing-Fork selected for
  Android. Device setup and end-to-end transport testing remain separate from
  the Courier module tests.

## Development quick start

For the server, install Python 3.12+ and uv. Run from the repository root:

```sh
uv sync --project server --locked
uv run --project server python -c 'import weaver'
uv run --project server python -m weaver.app --help
uv run --project server pytest
uv run --project server ruff check server
uv run --project server ruff format --check server
uv run --project server mypy server/src
```

For Android, install JDK 21 and an Android SDK with `platforms;android-36`.
Set `ANDROID_HOME` or configure `android/local.properties`, then run:

```sh
cd android
./gradlew assembleDebug lint testDebugUnitTest
```

When changing contracts, regenerate the schemas and update the handwritten
Kotlin models in the same change. See [schema/README.md](schema/README.md) for
export commands and [AGENTS.md](AGENTS.md) for development conventions.

## Tests and CI

Tests must not access real LLMs, speech models, or user vaults. PR validation
relies on passing local checks before opening the PR.

The [CI workflow](.github/workflows/ci.yml) runs full verification only through
manual `workflow_dispatch` or release tag pushes matching `v*`. Pull requests,
branch pushes, and schedules do not trigger CI. It checks the server on Ubuntu
and macOS arm64, schema export freshness, and Android lint, unit tests, and the
debug build. The macOS job verifies compatibility, not STT performance.

## Privacy principles

- Keep raw sources in local storage outside the vault. Sync only the four Courier
  folders; keep archives, server state, the vault, and credentials separate.
- Centralize LLM calls in `LlmClient`. Audio transcription is local, but text sent
  for LLM processing leaves the device; the planned revision step sends the full
  transcript. Automatic redaction is outside the MVP scope.
- Require human review before proposed facts reach Fabric and vault export.
- Keep API keys, signing keys, passwords, and personal configuration out of git.
