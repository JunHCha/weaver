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

Commit `server/uv.lock`. Runtime dependencies belong in `project.dependencies`,
pytest in the `test` group, and lint/type tools in `dev` (which includes `test`).
Use Ruff E4/E7/E9/F/I/B/UP and its formatter, and mypy strict targeting Python 3.12.
Mypy configuration lives at the root so the command above loads it automatically.
Tests must not access real LLMs, speech models, or user vaults.
Local setup rationale is in `docs/guides/server-development.md`; domain decisions
are in `docs/domain/decisions/` (these shared local folders are not committed).
