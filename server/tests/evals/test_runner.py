"""Hand-calculated metrics, replay integration and opt-in boundaries; no APIs."""

import copy
import json

import pytest
from typer.testing import CliRunner

from weaver.evals.__main__ import app
from weaver.evals.dataset import load_dataset
from weaver.evals.runner import (
    FIXTURES,
    ReplayClient,
    RunLedger,
    compare,
    evaluate_case,
    run,
    score_refiner,
)
from weaver.evals.scoring import aggregate, canonical, match, metric, score_extraction
from weaver.llm import BudgetExceeded, LlmTimeout
from weaver.llm.ledger import Ledger
from weaver.schema.internal import Extraction


@pytest.fixture(scope="module")
def cases():
    return load_dataset()


@pytest.fixture(scope="module")
def responses():
    return json.loads(FIXTURES.read_text())["cases"]


def test_hand_calculated_omission_extra_and_direction(cases):
    loaded = next(c for c in cases if c.case.case_id == "share-001")
    gold = loaded.extractions[0]
    data = gold.model_dump(mode="json", by_alias=True)
    # One of two entities missed, sole draft missed, reversed relation plus extra.
    data["entities"] = data["entities"][:1]
    data["entry_drafts"] = []
    data["relations"][0]["from"], data["relations"][0]["to"] = (
        data["relations"][0]["to"],
        data["relations"][0]["from"],
    )
    data["relations"].append(copy.deepcopy(data["relations"][0]))
    # Scorer accepts independently validated models even with dangling refs, so
    # restore omitted refs in relations/schedules via equivalent known IDs.
    predicted = Extraction.model_validate(data)
    scores = score_extraction(predicted, [gold], loaded.entity_ids)["metrics"]
    assert scores["entities.precision"] == metric(1, 1)
    assert scores["entities.recall"] == metric(1, 2)
    assert scores["entry_drafts.recall"] == metric(0, 1)
    assert scores["relations.precision"] == metric(0, 2)
    assert scores["relations.recall"] == metric(0, 1)
    assert scores["relations.missing"] == metric(1, 1)
    assert scores["relations.extra"] == metric(2, 2)


def test_ref_names_order_and_durable_ids_do_not_change_scores(cases):
    loaded = next(c for c in cases if c.case.case_id == "share-001")
    gold = loaded.extractions[0]
    raw = gold.model_dump(mode="json", by_alias=True)
    # Rename every local/durable ref in BOTH data/index; predictions reordered.
    mapping = {e.ref: f"x{i + 1}" for i, e in enumerate(gold.entities)}
    mapping.update({d.ref: f"y{i + 1}" for i, d in enumerate(gold.entry_drafts)})
    mapping.update({s.ref: f"z{i + 1}" for i, s in enumerate(gold.schedules)})
    mapping.update(
        {
            value: f"ent_{'0' * 25}{i}"
            for i, value in enumerate(loaded.entity_ids.values())
        }
    )

    def rename(v):
        if isinstance(v, str):
            return mapping.get(v, v)
        if isinstance(v, list):
            return [rename(x) for x in v]
        if isinstance(v, dict):
            return {k: rename(x) for k, x in v.items()}
        return v

    renamed = rename(raw)
    renamed_gold = Extraction.model_validate(renamed)
    for group in ("entities", "entry_drafts", "schedules", "relations"):
        renamed[group].reverse()
    prediction = Extraction.model_validate(renamed)
    new_ids = {k: mapping[v] for k, v in loaded.entity_ids.items()}
    assert canonical(prediction, new_ids) == canonical(gold, loaded.entity_ids)
    assert score_extraction(prediction, [renamed_gold], new_ids)["metrics"][
        "exact"
    ] == metric(1, 1)


def test_maximum_weight_one_to_one_not_greedy():
    p = [{"a": 1, "b": 1}, {"a": 1, "b": 2}]
    g = [{"a": 1, "b": 2}, {"a": 2, "b": 1}]
    assert match(p, g) == [(0, 1), (1, 0)]


def test_empty_alternatives_extra_predictions_and_micro_average(cases):
    loaded = next(c for c in cases if c.case.case_id == "share-001")
    assert score_extraction(None, [], {})["metrics"]["exact"] == metric(1, 1)
    extra = score_extraction(loaded.extractions[0], [], loaded.entity_ids)
    assert extra["metrics"]["exact"] == metric(0, 1)
    assert extra["metrics"]["entities.recall"] == metric(0, 0)
    assert extra["metrics"]["entities.precision"] == metric(0, 2)
    assert aggregate([{"x": metric(1, 2)}, {"x": metric(2, 3)}]) == {"x": metric(3, 5)}
    multi = next(c for c in cases if len(c.extractions) > 1)
    assert score_extraction(multi.extractions[-1], multi.extractions, multi.entity_ids)[
        "metrics"
    ]["exact"] == metric(1, 1)


def test_wrong_homonym_link_and_ask_are_visible(cases):
    loaded = next(c for c in cases if "homonym" in c.case.tags)
    gold = loaded.extractions[0]
    raw = gold.model_dump(mode="json", by_alias=True)
    entity = next(e for e in raw["entities"] if e["ask"])
    entity["entity_id"] = next(iter(loaded.entity_ids.values()))
    entity["ask"] = None
    scores = score_extraction(
        Extraction.model_validate(raw), [gold], loaded.entity_ids
    )["metrics"]
    assert scores["entities.field.entity_id"]["score"] < 1
    assert scores["entities.field.ask"]["score"] < 1


def test_unrelated_candidates_do_not_count_as_recall(cases):
    loaded = next(
        c
        for c in cases
        if c.reject_spans and c.candidates and c.case.input.stage != "extract"
    )
    start, end = loaded.reject_spans[0]
    unrelated = loaded.candidates[0].model_copy(
        update={"start_char": start, "end_char": end, "triggers": []}
    )
    scores = score_refiner(loaded, [unrelated])
    assert scores["refine.context_recall"]["numerator"] == 0
    assert scores["refine.trigger_recall"]["numerator"] == 0
    assert (
        scores["refine.reject_clear"]["numerator"]
        < scores["refine.reject_clear"]["denominator"]
    )


@pytest.mark.parametrize("failure", ["json", "schema", "call"])
def test_errors_preserve_case_and_denominators(cases, responses, failure):
    loaded = next(c for c in cases if c.case.case_id == "share-001")
    data = copy.deepcopy(responses[loaded.case.case_id])
    data["loom.extract"] = "{bad json" if failure == "json" else {"invalid": True}

    class FailClient(ReplayClient):
        def generate_json(self, *args, **kwargs):
            raise LlmTimeout()

    client = FailClient(data) if failure == "call" else ReplayClient(data)
    row = evaluate_case(loaded, client)
    assert row["case_id"] == loaded.case.case_id
    assert row["metrics"]["extract.exact"] == metric(0, 1)
    assert row["metrics"]["extract.success"] == metric(0, 1)
    assert row["metrics"]["extract.entities.recall"] == metric(0, 2)
    assert row["metrics"]["extract.entities.missing"] == metric(2, 2)
    assert row["errors"]
    assert row["calls"][0]["status"] != "succeeded"


def test_revision_failure_does_not_silently_use_gold(cases, responses):
    loaded = next(c for c in cases if c.case.case_id == "call-001")
    data = copy.deepcopy(responses[loaded.case.case_id])
    data["spinner.revise"] = {"segments": []}
    row = evaluate_case(loaded, ReplayClient(data))
    assert row["metrics"]["revise.exact"] == metric(0, 1)
    assert row["metrics"]["pipeline.exact"] == metric(0, 1)
    assert row["stages"]["pipeline"]["failed"]


def test_full_offline_report_and_regression(cases, responses):
    report = run(
        cases, lambda c: ReplayClient(responses[c.case.case_id]), backend="fixture"
    )
    assert report["sample_count"] == 32
    assert all(not c["errors"] for c in report["cases"])
    assert report["metrics"]["refine.context_recall"] == metric(32, 33)
    assert report["metrics"]["refine.trigger_recall"] == metric(30, 33)
    assert report["metrics"]["revise.lines"] == metric(165, 165)
    assert "tag:homonym" in report["categories"]
    assert all(c["stages"]["extract"]["matches"] is not None for c in report["cases"])
    assert not compare(report, report)["regressed"]
    baseline = copy.deepcopy(report)
    baseline["cases"][0]["metrics"]["extract.exact"] = metric(1, 1)
    report["cases"][0]["metrics"]["extract.exact"] = metric(0, 1)
    comparison = compare(report, baseline)
    assert comparison["regressed"]
    assert any(
        d["scope"].startswith("case:") and d["regression"]
        for d in comparison["differences"]
    )
    assert not compare(report, baseline, tolerance=1)["regressed"]
    baseline["dataset_hash"] = "different"
    with pytest.raises(ValueError, match="incompatible"):
        compare(report, baseline)


def test_run_budget_is_separate_from_shared_monthly_cap(tmp_path):
    shared = Ledger(tmp_path / "shared.db")
    limited = RunLedger(
        shared, Ledger(tmp_path / "run.db", warn_usd=0.01, stop_usd=0.01)
    )
    record = {"status": "pending", "cost_usd": None}
    call_id = limited.reserve(record.copy(), 6_000_000)
    limited.finish(call_id, record.copy(), 6_000_000)
    with pytest.raises(BudgetExceeded):
        limited.reserve(record.copy(), 6_000_000)
    assert sum(r["budget_held_usd"] for r in shared.records()) == pytest.approx(0.006)
    # A second run cannot bypass the shared monthly cap.
    monthly = Ledger(tmp_path / "monthly.db", warn_usd=0.001, stop_usd=0.001)
    limited = RunLedger(monthly, Ledger(tmp_path / "other.db"))
    with pytest.raises(BudgetExceeded):
        limited.reserve(record.copy(), 2_000_000)


def test_cli_offline_baseline_and_live_preflight(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = CliRunner()
    output = tmp_path / "report.json"
    result = runner.invoke(app, ["run", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.with_suffix(".txt").is_file()
    report = json.loads(output.read_text())
    assert report["cost_usd"] == 0
    result = runner.invoke(
        app,
        ["run", "--baseline", str(output), "--output", str(tmp_path / "second.json")],
    )
    assert result.exit_code == 0, result.output
    report["metrics"]["refine.context_recall"] = metric(33, 33)
    output.write_text(json.dumps(report))
    result = runner.invoke(
        app,
        [
            "run",
            "--baseline",
            str(output),
            "--output",
            str(tmp_path / "regressed.json"),
        ],
    )
    assert result.exit_code == 1
    assert "REGRESSION" in result.output

    def forbidden(*args, **kwargs):
        raise AssertionError("must not load config or construct a live client")

    monkeypatch.setattr("weaver.evals.__main__.load_config", forbidden)
    for args in (
        ["--live"],
        ["--live", "--max-cost-usd", "1"],
        ["--live", "--max-cost-usd", "nan", "--confirm-budget"],
    ):
        result = runner.invoke(
            app, ["run", *args, "--output", str(tmp_path / "live.json")]
        )
        assert result.exit_code == 2
    assert not (tmp_path / "live.json").exists()


def test_two_models_and_prompt_settings_have_comparable_category_deltas(
    cases, responses, monkeypatch
):
    from weaver.loom import extract

    loaded = next(c for c in cases if c.case.case_id == "share-001")
    first = ReplayClient(responses[loaded.case.case_id])
    first.model = "synthetic-model-a"
    before = run([loaded], lambda _: first, backend="fixture")
    changed = copy.deepcopy(responses[loaded.case.case_id])
    for response in changed["loom.extract"]["by_input"].values():
        response["schedules"][0]["start"] = "2031-03-13T14:00:00+09:00"
    second = ReplayClient(changed)
    second.model = "synthetic-model-b"
    monkeypatch.setattr(
        extract, "_SYSTEM", extract._SYSTEM + "\nSynthetic prompt variation."
    )
    after = run([loaded], lambda _: second, backend="fixture")
    assert before["models"] != after["models"]
    assert before["prompt_hashes"] != after["prompt_hashes"]
    differences = compare(after, before)["differences"]
    assert any(d["scope"] == "tag:attendance" and d["regression"] for d in differences)
    assert any(d["scope"] == "case:share-001" and d["regression"] for d in differences)


def test_malformed_baseline_is_rejected_before_client_construction(tmp_path):
    baseline = tmp_path / "baseline.json"
    for malformed in ({}, [], {"report_version": 1}):
        baseline.write_text(json.dumps(malformed))
        result = CliRunner().invoke(
            app,
            [
                "run",
                "--baseline",
                str(baseline),
                "--live",
                "--max-cost-usd",
                "1",
                "--confirm-budget",
            ],
        )
        assert result.exit_code == 2
        assert "incompatible baseline" in result.output


def test_ref_normalization_preserves_non_reference_strings(cases):
    loaded = next(c for c in cases if c.case.case_id == "share-001")
    raw = loaded.extractions[0].model_dump(mode="json", by_alias=True)
    raw["entities"][0]["name"] = "e1"
    raw["entry_drafts"][0]["field"] = "e1"
    rows = canonical(Extraction.model_validate(raw), loaded.entity_ids)
    assert any(entity["name"] == "e1" for entity in rows["entities"])
    assert rows["entry_drafts"][0]["field"] == "e1"
