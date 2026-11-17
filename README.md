# Weaver

Turn everyday phone records into reviewed personal knowledge.

Weaver is a personal tool for collecting manually shared Galaxy call transcript
text, KakaoTalk exports, and shared text. A local Mac server parses the material
and uses an LLM to write one event summary per source. You review each summary
once on the phone; only approved summaries enter an append-only log, from which a
personal knowledge vault such as Obsidian is compiled.

**Code merged; real-device validation pending:** automated tests cover the
server workflow with fake LLMs and temporary paths. Follow the
[roadmap (#37)](https://github.com/JunHCha/weaver/issues/37).

## How it works

The names follow a weaving metaphor: **Shepherd** captures material on the phone,
**Courier** transports files through Syncthing, **Spinner** parses a source into a
Script, **Loom** drafts the event summary and applies your review, **Fabric** keeps
the approved facts as a log, and **Wiki** compiles that log into the vault.

The diagram shows the implemented flow; device-to-device validation is still
pending. Courier's four folders carry files in the directions shown; they are
transport routes, not the raw archive or the knowledge vault.

```mermaid
%%{init: {"htmlLabels": false}}%%
flowchart TB
  subgraph phone["Phone · Android"]
    shepherd["Shepherd<br/>Share call script text, KakaoTalk, text"]
    review["Final review, once<br/>Approve · Edit text · Feedback · Reject"]
    calendar["Calendar executor<br/>Automatic; you pick the calendar once"]
  end

  subgraph sync["Sync · Courier / Syncthing"]
    outbox["outbox/<br/>Captures + payloads"]
    receipts["receipts/<br/>Archive acknowledgements"]
    proposals["proposals/<br/>Review packages, alerts"]
    decisions["decisions/<br/>Verdicts"]
    actions["actions/ · action-results/<br/>Calendar commands and their results"]
  end

  subgraph mac["Mac server · Local processing and storage"]
    direction TB
    archive["SourceStore<br/>Raw archive outside the vault"]
    spinner["Spinner<br/>Parse + call revision → Script"]
    draft["Loom · draft<br/>Event summary + up to 3 questions"]
    apply["Loom · apply<br/>Approved summary → Threads"]
    summaries[("Event summaries<br/>Content-addressed")]
    llm["LlmClient<br/>Anthropic API or Claude CLI"]
    fabric[("Fabric<br/>Append-only log with seq")]
    alerts["Alert detector<br/>Moved approved schedules"]
    engine["Action engine<br/>Fabric seq cursor → outbox"]
    actionbox[("Action outbox<br/>Commands and execution state")]
    wiki["Wiki<br/>Body by LLM, frontmatter by code"]
    digest["Digest + query pages<br/>Day · week · month, open requests"]
  end

  vault["Personal knowledge vault<br/>Weaver folder of Markdown"]

  shepherd --> outbox --> archive
  archive --> receipts --> shepherd
  archive -- "Call scripts / KakaoTalk / shared text" --> spinner
  spinner -- "Script" --> draft
  spinner -. "Call revision" .-> llm
  draft -. "Draft" .-> llm
  apply -. "Facts, feedback patches" .-> llm
  draft --> proposals --> review
  review --> decisions --> apply
  apply -- "Feedback: next revision" --> proposals
  apply -- "Approve / edit" --> summaries
  apply -- "Approve / edit / reject" --> fabric
  fabric --> wiki --> vault
  fabric --> digest --> vault
  vault -- "Your page edits, after 30 min unchanged" --> wiki
  wiki -- "Meaningful edits as human Threads" --> fabric
  fabric --> proposals
  fabric --> alerts --> proposals
  fabric -- "Schedules" --> engine --> actionbox --> actions
  actions -- "register / update / remove" --> calendar
  calendar -- "Result" --> actions
  actions -- "실행 Thread" --> fabric

  classDef capture fill:#dbeafe,stroke:#2563eb,color:#172554
  classDef transport fill:#fef3c7,stroke:#b45309,color:#451a03
  classDef process fill:#ede9fe,stroke:#7c3aed,color:#2e1065
  classDef human fill:#fce7f3,stroke:#be185d,color:#500724
  classDef stored fill:#dcfce7,stroke:#15803d,color:#052e16
  class shepherd capture
  class outbox,receipts,proposals,decisions,actions transport
  class spinner,draft,apply,llm,alerts,engine,wiki,digest process
  class review,calendar human
  class archive,summaries,fabric,actionbox,vault stored
```

Call input is manually shared Galaxy recording transcript text (ADR-028).
Spinner does not invoke STT. LLM revision, drafting and extraction use external
APIs through the shared `LlmClient` boundary. Approving a summary records facts.
It does not authorize execution of requests found in the source material.

| Term | Meaning |
| --- | --- |
| Script | A source's parsed and polished text, with segments and provenance retained. |
| Event summary | The single document Loom drafts for one source (the review unit). The approved text is stored by its content hash. |
| Verdict | The review result for one exact revision: approve, edit the text, natural-language feedback, or reject. |
| Thread | One row of the Fabric log: an occurrence time, one sentence of content, tags, a kind (request, decision, schedule, record), source and actor. |
| Action | A command for the outside world (today: `calendar.register`, `calendar.update`, `calendar.remove`). It lives in the Action outbox, not in Fabric, because its execution state changes. |
| 실행 Thread | The recorded result of an Action (done, or given up, with the calendar event ID). It points at the Thread it executed with `executes`, and wiki, Digest and the data-flow map do not treat it as a business fact. |
| Fabric | The append-only log of approved or human-written Threads. The store assigns a monotonic `seq`; consumers read from a `seq` bookmark. Corrections add rows (a tag correction, a rejection) instead of rewriting history. |
| Wiki | The vault pages compiled from Fabric: the LLM writes each page body, code writes the frontmatter. |
| Digest | Generated day, week and month summaries with links back to their sources. |
| Query pages | Deterministic views such as open requests, upcoming deadlines and contacts by person. |
| Context dictionary | Names, aliases, known misrecognitions and kinds (person, organization, project, topic) used as drafting hints; it starts empty and learns the corrections you choose. |
| Fleece | Raw audio. Not collected in the current flow (ADR-028); the term is kept for the optional, unwired STT adapter. |

See the [JSON contracts](schema/README.md) for the wire formats and review rules.

## Your data at a glance

The Android first screen maps each item across **Shepherd · Spinner · Loom · Fabric · Wiki**
and **You**. Tap a node for its items or open **You** for pending approvals. Landscape
windows keep the map beside a right panel; portrait windows use a bottom sheet.
The map replays changes since the last visit once, with no live movement loop.

The server publishes additive progress observations under `proposals/pipeline/`;
existing archive receipts confirm Mac arrival. Today's Thread results, wiki pages
and Digest link back to each item. Select the phone's synchronized local vault and
enter its Obsidian name to enable page links after matching file bytes arrive.
See [the data flow map](docs/pipeline-map.md) for state and synchronization rules.

## Current status

Merged implementation:

- Resident server runtime with restart recovery and launchd management via
  `python -m weaver.app` subcommands `run`, `install-agent`, `uninstall-agent`,
  and `status`; Courier file handling and SourceStore archiving.
- Spinner text parsing, Galaxy call revision, persistent checkpoints and retries;
  Loom event summary drafting with bounded questions, one-time review
  (approve, edit, feedback, reject), content-addressed approved summaries and
  recoverable Thread extraction.
- Append-only SQLite Fabric with deterministic current-value projection;
  schedule change alerts and approved-schedule files for the phone.
- Wiki compilation with protected pages, human-edit capture (diffs are committed
  to a git directory outside the vault and meaningful changes become Threads),
  Digest and query pages, all disabled until `[wiki]` is configured.
- Android Shepherd call metadata and manual text sharing, Storage Access
  Framework (SAF) Courier transport, review and change-alert notifications, one
  review screen (summary first, choice questions, processing steps), a Glance
  widget, a data flow home map with per-item progress and result links, and
  approved schedule registration through CalendarContract with
  interruption recovery.
- Offline summary evaluation replay and server E2E smoke tests.

The code is merged, but the full flow connecting a real Galaxy Fold,
Syncthing, and a real LLM has **not been validated**. Validation so far is limited
to automated tests using fake LLMs and temporary paths. Call input uses manually
shared Galaxy transcript text; the optional local STT adapter remains in the
code but is not connected to Spinner. Syncthing setup research
([#19](https://github.com/JunHCha/weaver/issues/19)) remains open; audio-file
cleanup ([#32](https://github.com/JunHCha/weaver/issues/32)) is deferred.

## Repository and stack

| Path | Contents |
| --- | --- |
| [server/](server/) | Python server modules and tests. |
| [android/](android/) | Android app and contract tests. |
| [schema/](schema/) | Generated JSON Schemas and synthetic examples. |
| [.github/workflows/](.github/workflows/) | Manual and release verification. |

- **Server:** Python 3.12+, uv, Typer, SQLite, Ruff, and strict mypy.
- **Speech (optional):** the local MLX Whisper `large-v3-turbo` adapter remains
  available through the `stt` installation extra on Apple Silicon, but is not
  wired into Spinner. It requires model preparation and `ffmpeg`, and does not
  identify speakers.
- **LLM:** A shared `LlmClient` boundary with an Anthropic Claude adapter today;
  [more backends are planned (#46)](https://github.com/JunHCha/weaver/issues/46).
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
and macOS arm64, the offline summary evaluation replay, schema export freshness, and
Android lint, unit tests, and the debug build. The macOS job verifies
compatibility, not STT performance.

## Privacy principles

- Keep raw sources in local storage outside the vault. Sync only the four Courier
  folders for transport; keep archives, server state, and credentials separate.
  To open generated pages on the phone, separately share the intended vault
  through Syncthing and grant Weaver read access to its local folder.
- Centralize LLM calls in `LlmClient`. The current input is text, not raw audio;
  text sent for revision, drafting and extraction leaves the device. Call revision sends
  speech segments from the shared transcript. Automatic redaction is not provided.
- Require human review before extracted facts reach Fabric and the vault.
- Keep API keys, signing keys, passwords, and personal configuration out of git.

The wiki is disabled by default. To enable periodic compilation, add the
following to `server.toml` (paths are relative to the configuration file):

```toml
[wiki]
enabled = true
# vault_root = "vault" # optional; defaults to the top-level vault_root
folder = "Weaver"
timezone = "Asia/Seoul"
settle_seconds = 1800
interval_seconds = 60
```

The settle period protects recent human edits; pending pages are retried on a
later wiki interval. Keep the state directory outside the wiki vault.

Start at `Weaver/index.md`. Wiki generation reads the complete Fabric snapshot and
approved summaries in one structured LLM call, organizing pages around projects,
operations and reusable knowledge rather than creating a page for every tag.
`schema.md` records the operating rules; `log.md` records the latest compilation
sequence and any explicitly excluded evidence. Existing human page edits remain
protected, and rebuilding the wiki does not re-extract or rewrite Fabric facts.

Source summaries use Obsidian-safe filenames. When a source title changes, the
server repairs links and retires its old summary copy and restoration record;
pending human edits are preserved for a later retry.

Wiki edits also appear on the map as teal items with a **Wiki → Fabric → Wiki**
route and change/Thread/result details. They do not create capture receipts or
approval tasks. The server reports committed updates; the Android app separately
checks arrival in the phone's synchronized vault before opening a result.

### 서버 웹 UI

`pnpm --dir web install --frozen-lockfile && pnpm --dir web build` 후
`uv run --project server python -m weaver.app web --config /path/to/config.toml`을
실행하면 `http://127.0.0.1:8765`에서 Fabric·Spinner·Loom을 읽기 전용으로
확인할 수 있습니다. `weaver web`은 loopback 호스트만 허용하며 쓰기 API가
없습니다. `--port`, `--static-dir` 또는 `WEAVER_WEB_DIR`로 포트와 정적 파일
위치를 지정할 수 있고, 빌드가 없으면 경고와 함께 API만 제공합니다.

### CLI LLM 제한 시간과 재시도

[`server/config.example.toml`](server/config.example.toml)의 `[llm]`에서
Claude CLI와 Codex CLI의 제한 시간을 설정한다.

```toml
[llm]
backend = "codex_cli" # 또는 "claude_cli"
cli_timeout_seconds = 300
wiki_cli_timeout_seconds = 600
max_cli_calls_per_day = 30
```

두 제한 시간은 초 단위이며 0보다 크고 900 이하여야 한다. 일반 호출은 기본
300초, `wiki.compile_batch`·`wiki.digest` 등 `wiki.*` 호출은 기본 600초를 쓴다.
API 백엔드는 기존 120초 제한과 예산 규칙을 유지한다.

소스 정리와 Wiki 작업이 연속으로 시간 초과되면 각 작업의 재시도 간격을
기본 주기의 2배, 4배, 8배로 늘리며 최대 1시간을 기다린다. 성공하면 초기화한다.
소스별 대기 시각과 연속 시간 초과 횟수는 재시작 뒤에도 유지하고, Wiki의
재시도 간격은 프로세스 안에서 관리한다. 소스의 기존 최대 20회 재시도 규칙은 유지한다.

두 CLI가 공유하는 일일 호출 한도의 절반 이상을 시간 초과로 사용하면 그날의
Wiki CLI 호출을 중지한다. 예를 들어 한도가 30이면 시간 초과 15회부터 중지하며,
홀수 한도는 올림한다. 이 집계에는 소스 호출의 시간 초과도 포함한다. 집계는
`state_root/llm.sqlite3`에 보관해 재시작 뒤에도 적용하며, 호출을 시작한 서버 현지
날짜를 기준으로 다음 날 다시 허용한다. 소스 처리는 남은 일일 한도 안에서 계속한다.
경고 로그에는 원문·프롬프트·응답을 남기지 않는다.

Codex 시간 초과 경고에는 마지막 `--json` 이벤트 유형(`last_event`), 경과 초,
이벤트 스트림 크기(`stdout_bytes`), 최종 출력 파일 크기(`output_bytes`),
직렬화한 입력과 스키마 크기(`input_bytes`, `schema_bytes`)를 남긴다.
같은 수치를 호출 ledger의 `timeout_diagnostics`에도 저장한다. 이벤트 이름은
허용 목록으로 제한하고 알 수 없는 유형은 `unknown`, 이벤트가 없으면 `none`으로
기록한다. 이 수치는 입력·스키마 규모와 마지막으로 관찰한 진행 단계를 보여주며,
모델 내부 추론이나 스키마 처리 중 어느 쪽이 원인인지를 확정하지는 않는다.
