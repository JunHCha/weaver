# Evals

An offline regression check for the event summary prompts. It replays recorded
provider responses through the production draft and approval stages, so it needs
no LLM, speech model or user vault, and it is separate from the pytest unit
tests.

```sh
uv run --project server python -m weaver.evals summary
uv run --project server python -m weaver.evals summary \
  --cases server/evals/summary_cases.json --output reports/summary-evals.json
uv run --project server pytest server/tests/evals
```

`summary_cases.json` holds independent cases. Each case has a `script`, the
reviewer's `answers`, the provider `responses` per task (`loom.summary`,
`loom.approve`) and the `expected` draft body, questions and extracted Threads
(`kind`, `content`, `tags`). The runner builds a `ReviewPackage` with
`SummaryDrafts`, approves it with `SummaryReview` against a fake Thread sink in a
temporary directory, and writes predictions and call traces (task, prompt hash,
status, latency) to the report. Exit code 0 means every metric passed (`draft.exact`,
`questions.exact`, `threads.exact`), 1 a metric mismatch, 2 an input error.

The responses are fixtures, not model output; they guard the runner and the
wiring, not model quality. To judge a prompt change against a real model, run
the production flow with a real `LlmClient` yourself, since this command never
calls a provider. `llm_smoke.py` is the explicit, paid two-request smoke check
for the configured backend and is never collected by pytest.

Use synthetic names such as 홍길동 and 김철수 and 203x dates in new cases; never
copy real conversations.

## Approval rules (#147)

```sh
uv run --project server python -m weaver.evals approval --output reports/approval-replay.json
uv run --project server pytest server/tests/evals server/tests/loom server/tests/context
```

`approval_cases.json` contains three synthetic approval scenarios: correction of
김철쑤 to 김철수 without a typo alias; review-only interpretation/identity Q&A
without an extra Thread; and every involved person/organization/project tagged
using names without titles. A fourth bootstrap scenario checks automatic title
removal, plan-only typo/spacing mappings and absence of former-name aliases.
Replay checks production wiring and deterministic guards, **not model quality**;
returning the right canned response cannot establish that a prompt obeys a rule.
Prompt contract tests assert that approval/bootstrap/wiki instructions contain
these rules. No server heuristic classifies Thread prose as interpretation.

A human can explicitly run a paid real-model evaluation later:

```sh
uv run --project server python server/evals/approval_live.py \
  --config /path/to/synthetic-eval.toml --output reports/approval-live.json
```

This makes four requests using only the synthetic fixture inputs and the selected
config's LLM settings. It creates dictionary, state and accounting in temporary
directories; it never opens the config's vault/archive/state folders. The normal
backend may use its configured credentials or subscription client's auth/cache.
This script is never run by pytest or by replay. Its temporary ledger is isolated
from the daemon's monthly budget, so use a dedicated eval budget configuration.
Metrics check fact count, involved tags, title absence, disallowed meta fragments,
forbidden typo aliases and explicit corrections; bootstrap checks rename and
mapping proposals. Reports include full synthetic predictions and prompt hashes.
Inspect the predicted text too: the fragment metrics cannot recognize every
paraphrase of a spurious interpretation fact. Do not replace these inputs with
real conversations or a user vault to run this regression set.
