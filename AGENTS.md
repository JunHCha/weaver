# Development

Server requires Python >=3.12 and uv. Run from the repository root:

```sh
uv sync --project server --locked
uv run --project server python -c 'import weaver'
uv run --project server python -m weaver.app --help
uv run --project server pytest
uv run --project server ruff check server
uv run --project server ruff format --check server
uv run --project server mypy server/src
```

Use Typer for the server CLI.

Commit `server/uv.lock`. Runtime dependencies belong in `project.dependencies`,
pytest in the `test` group, and lint/type tools in `dev` (which includes `test`).
Use Ruff E4/E7/E9/F/I/B/UP and its formatter, and mypy strict targeting Python 3.12.
Mypy configuration lives at the root so the command above loads it automatically.
Tests must not access real LLMs, speech models, or user vaults.
Local setup rationale is in `docs/guides/server-development.md`; domain decisions
are in `docs/domain/decisions/` (these shared local folders are not committed).

## Android

Requires JDK 21 and an Android SDK with `platforms;android-36` (set `ANDROID_HOME` or
`android/local.properties`). Run from `android/`:

```sh
./gradlew assembleDebug lint testDebugUnitTest
```

Kotlin models in `android/app/src/main/kotlin/.../schema/` are hand-written
kotlinx.serialization classes; unit tests validate them against the repository-root
`schema/` files, so regenerate the schema first and update the models in the same change.
Release signing reads the key from `~/.config/weaver/android/` and passwords from
`WEAVER_ANDROID_*` environment variables; never commit keys, passwords or their copies.
Version rationale and signing procedures are in `docs/guides/android-development.md` and
`docs/guides/android-signing.md` (local only).

## CI

PR validation relies on the worker passing the local checks above before opening a PR.
`.github/workflows/ci.yml` runs full verification when preparing release notes, via
manual `workflow_dispatch` or a release tag push matching `v*`. PRs, branch pushes
(including main) and schedules do not trigger CI. Every run includes server checks
on Ubuntu and macOS arm64 with Python 3.12, the pinned uv and `server/uv.lock`
(plus a check that `weaver.schema.export` matches committed `schema/*.schema.json`),
and Android `./gradlew --no-daemon lint testDebugUnitTest assembleDebug`.
The macOS job checks compatibility, not M1 STT performance. `CI result` always
runs and fails if any verification job is skipped, failed or cancelled; do not
require it on PRs, where no CI run is expected.
Pin new actions to a full commit SHA with the tag in a comment; CI uses no secrets.
