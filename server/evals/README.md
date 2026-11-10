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
