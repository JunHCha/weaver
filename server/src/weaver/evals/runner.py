"""Offline replay and opt-in LlmClient evaluation of real pipeline components."""

import hashlib
import inspect
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from weaver.llm import InvalidJson, SchemaMismatch
from weaver.llm.ledger import Ledger
from weaver.llm.types import LlmClient
from weaver.loom import extract, refine
from weaver.loom.extract import LlmExtractor
from weaver.loom.refine import RuleRefiner
from weaver.schema import Thread
from weaver.schema.internal import ArchivedSource, Candidate, Extraction
from weaver.spinner import Spinner, pipeline, reviser
from weaver.spinner.reviser import LlmReviser

from .dataset import LoadedCase, ReviseCall
from .scoring import Metric, aggregate, metric, score_extraction

FIXTURES = Path(__file__).resolve().parents[3] / "evals" / "fixtures.json"


def digest(data: Any) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ReplayClient:
    """Versioned, independent provider responses; no expected-answer lookup."""

    model = "fixture.v1"

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        response = self.responses[task]
        if isinstance(response, dict) and "by_input" in response:
            response = response["by_input"][digest(json.loads(user))]
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                raise InvalidJson() from None
        try:
            return schema.model_validate(response)
        except ValidationError:
            raise SchemaMismatch() from None

    def generate_text(self, task: str, system: str, user: str) -> str:
        raise ValueError("unexpected text request")


class RunLedger(Ledger):
    """Reserve every attempt in BOTH the shared monthly and run budget ledgers."""

    def __init__(self, shared: Ledger, run: Ledger) -> None:
        self.shared, self.run = shared, run
        self.calls: dict[str, str] = {}

    def reserve(self, record: dict[str, Any], amount: int) -> str:
        shared_id = self.shared.reserve(dict(record), amount)
        try:
            run_id = self.run.reserve(record, amount)
        except Exception:
            self.shared.finish(shared_id, record, 0)
            raise
        self.calls[run_id] = shared_id
        return run_id

    def finish(self, call_id: str, record: dict[str, Any], charge: int) -> None:
        self.shared.finish(self.calls[call_id], record, charge)
        self.run.finish(call_id, record, charge)

    def records(self) -> list[dict[str, Any]]:
        return self.run.records()


class TracedClient:
    def __init__(self, client: LlmClient) -> None:
        self.client = client
        self.model = str(getattr(client, "model", "unknown"))
        self.calls: list[dict[str, Any]] = []

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        started = time.perf_counter()
        ledger = getattr(self.client, "ledger", None)
        before = len(ledger.records()) if isinstance(ledger, RunLedger) else 0
        record: dict[str, Any] = {
            "task": task,
            "prompt_hash": digest([system, user, schema.model_json_schema()]),
            "cost_usd": 0 if isinstance(self.client, ReplayClient) else None,
        }
        self.calls.append(record)
        try:
            result = self.client.generate_json(task, system, user, schema)
            record["status"] = "succeeded"
            return result
        except Exception as error:
            record["status"] = type(error).__name__
            raise
        finally:
            record["elapsed_seconds"] = time.perf_counter() - started
            if isinstance(ledger, RunLedger):
                new_records = ledger.records()[before:]
                record["cost_usd"] = (
                    new_records[-1].get("cost_usd") if new_records else 0
                )
                record["cost_source"] = (
                    new_records[-1].get("cost_source", "unavailable")
                    if new_records
                    else "no_dispatch"
                )

    def generate_text(self, task: str, system: str, user: str) -> str:
        return self.client.generate_text(task, system, user)


def score_refiner(loaded: LoadedCase, candidates: list[Candidate]) -> dict[str, Metric]:
    gold = loaded.candidates
    covered = sum(
        any(
            p.start_char <= g.start_char and g.end_char <= p.end_char
            for p in candidates
        )
        for g in gold
    )
    triggered = sum(
        any(
            t.start_char < g.end_char and g.start_char < t.end_char
            for p in candidates
            for t in p.triggers
        )
        for g in gold
    )
    rejected = sum(
        any(p.start_char < end and start < p.end_char for p in candidates)
        for start, end in loaded.reject_spans
    )
    return {
        "refine.context_recall": metric(covered, len(gold)),
        "refine.trigger_recall": metric(triggered, len(gold)),
        "refine.reject_clear": metric(
            len(loaded.reject_spans) - rejected, len(loaded.reject_spans)
        ),
        "refine.empty_correct": metric(int(not candidates), int(not gold))
        if not gold
        else metric(0, 0),
        "refine.selected_characters": metric(
            sum(p.end_char - p.start_char for p in candidates), len(loaded.thread.text)
        ),
    }


def _revision_metrics(loaded: LoadedCase, thread: Thread | None) -> dict[str, Metric]:
    expected = loaded.case.expected.revise
    edits = (
        {e.from_text: e.to for e in expected.edits}
        if isinstance(expected, ReviseCall)
        else {}
    )
    gold_lines = loaded.raw_thread.text.splitlines()
    predicted = thread.text.splitlines() if thread else []
    correct = sum(
        i < len(predicted) and predicted[i] in edits.get(line, [line])
        for i, line in enumerate(gold_lines)
    )
    return {
        "revise.lines": metric(correct, max(len(gold_lines), len(predicted))),
        "revise.exact": metric(
            int(len(gold_lines) == len(predicted) and correct == len(gold_lines)), 1
        ),
    }


def evaluate_case(loaded: LoadedCase, client: LlmClient) -> dict[str, Any]:
    traced = TracedClient(client)
    extractor = LlmExtractor(traced, timezone=loaded.case.reference.timezone)
    metrics: dict[str, Metric] = {}
    stages: dict[str, Any] = {}
    errors: list[dict[str, str]] = []

    def extraction_stage(
        name: str, thread: Thread | None, candidates: list[Candidate]
    ) -> None:
        prediction: Extraction | None = None
        failed = thread is None
        if thread is not None:
            try:
                prediction = extractor.extract(thread, candidates, loaded.known)
            except Exception as error:
                failed = True
                errors.append({"stage": name, "type": type(error).__name__})
        report = score_extraction(prediction, loaded.extractions, loaded.entity_ids)
        if failed:
            # Failures never become successful empty-answer cases or field hits.
            for metric_name, value in report["metrics"].items():
                if metric_name.endswith((".missing", ".extra")):
                    continue
                value["numerator"] = 0
                if value["denominator"]:
                    value["score"] = 0.0
            report["metrics"]["exact"] = metric(0, 1)
        metrics.update({f"{name}.{k}": v for k, v in report["metrics"].items()})
        metrics[f"{name}.success"] = metric(int(not failed), 1)
        stages[name] = {
            **report,
            "failed": failed,
            "raw_prediction": prediction.model_dump(mode="json", by_alias=True)
            if prediction
            else None,
        }

    extraction_stage("extract", loaded.thread, loaded.candidates)
    if loaded.case.input.stage != "extract":
        selected = RuleRefiner().select(loaded.thread)
        metrics.update(score_refiner(loaded, selected))
        stages["refine"] = [c.model_dump(mode="json") for c in selected]
    if loaded.case.input.stage == "spin":
        thread: Thread | None = loaded.raw_thread
        try:
            with TemporaryDirectory(prefix="weaver-eval-") as temporary:
                root = Path(temporary)
                payload = root / "input.txt"
                payload.write_text(loaded.case.input.text, encoding="utf-8")
                archived = ArchivedSource(source=loaded.source, payload_path=payload)

                class Locator:
                    def locate(self, source_id: str) -> ArchivedSource:
                        if source_id != archived.source.source_id:
                            raise ValueError("unexpected source")
                        return archived

                thread = Spinner(
                    Locator(),
                    root / "state",
                    configuration_version="eval.v1",
                    timezone=ZoneInfo(loaded.case.reference.timezone),
                    reviser=LlmReviser(traced),
                ).spin(loaded.source)
        except Exception as error:
            errors.append({"stage": "revise", "type": type(error).__name__})
            thread = None
        metrics.update(_revision_metrics(loaded, thread))
        metrics["revise.success"] = metric(int(thread is not None), 1)
        stages["revise"] = thread.model_dump(mode="json") if thread else None
        extraction_stage(
            "pipeline", thread, RuleRefiner().select(thread) if thread else []
        )
        if loaded.case.channel == "call":
            extraction_stage(
                "raw_pipeline",
                loaded.raw_thread,
                RuleRefiner().select(loaded.raw_thread),
            )
    return {
        "case_id": loaded.case.case_id,
        "channel": loaded.case.channel,
        "tags": loaded.case.tags,
        "input_stage": loaded.case.input.stage,
        "metrics": metrics,
        "stages": stages,
        "errors": errors,
        "calls": traced.calls,
        "model": traced.model,
        "configuration": {
            "timezone": loaded.case.reference.timezone,
            "refiner": {"before": 1, "after": 1},
            "llm": {
                name: getattr(client, name, None)
                for name in ("max_tokens", "deadline_seconds", "structured_output")
            },
        },
    }


def run(
    cases: list[LoadedCase],
    factory: Callable[[LoadedCase], LlmClient],
    *,
    backend: str,
    fixture_hash: str | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    rows = []
    for loaded in cases:
        started = time.perf_counter()
        row = evaluate_case(loaded, factory(loaded))
        row["elapsed_seconds"] = time.perf_counter() - started
        rows.append(row)
    categories = sorted(
        {f"channel:{r['channel']}" for r in rows}
        | {f"tag:{tag}" for r in rows for tag in r["tags"]}
    )
    return {
        "report_version": 1,
        "scoring_version": "v1",
        "run_id": uuid4().hex,
        "started_at": started_at,
        "sample_count": len(rows),
        "dataset_versions": sorted({c.case.schema_version for c in cases}),
        "dataset_hash": digest(
            [c.case.model_dump(mode="json", by_alias=True) for c in cases]
        ),
        "backend": backend,
        "models": sorted({r["model"] for r in rows}),
        "fixture_hash": fixture_hash,
        "prompts": {
            "extract": extract.PROMPT_VERSION,
            "revise": reviser.PROMPT_VERSION,
        },
        "implementation_hash": digest(
            [
                inspect.getsource(module)
                for module in (extract, refine, reviser, pipeline)
            ]
        ),
        "prompt_hashes": {
            "extract": digest(extract._SYSTEM),
            "revise": digest(reviser.SYSTEM_PROMPT),
        },
        "metrics": aggregate([r["metrics"] for r in rows]),
        "categories": {
            category: {
                "sample_count": len(
                    selected := [
                        r
                        for r in rows
                        if category == f"channel:{r['channel']}"
                        or category.removeprefix("tag:") in r["tags"]
                    ]
                ),
                "metrics": aggregate([r["metrics"] for r in selected]),
            }
            for category in categories
        },
        "cases": rows,
    }


def compare(
    report: dict[str, Any], baseline: dict[str, Any], tolerance: float = 0
) -> dict[str, Any]:
    for key in ("report_version", "scoring_version", "dataset_hash"):
        if report[key] != baseline[key]:
            raise ValueError(f"incompatible baseline: {key}")
    differences: list[dict[str, Any]] = []

    def differences_for(
        scope: str, current: dict[str, Any], previous: dict[str, Any]
    ) -> None:
        for name, old in previous.items():
            if name.endswith((".missing", ".extra", "selected_characters")):
                continue
            new = current.get(name)
            if old["score"] is None:
                continue
            delta = (
                None
                if new is None or new["score"] is None
                else new["score"] - old["score"]
            )
            differences.append(
                {
                    "scope": scope,
                    "metric": name,
                    "delta": delta,
                    "regression": delta is None or delta < -tolerance - 1e-12,
                }
            )

    differences_for("overall", report["metrics"], baseline["metrics"])
    for category, old in baseline["categories"].items():
        differences_for(
            category,
            report["categories"].get(category, {}).get("metrics", {}),
            old["metrics"],
        )
    previous = {r["case_id"]: r for r in baseline["cases"]}
    for row in report["cases"]:
        differences_for(
            f"case:{row['case_id']}",
            row["metrics"],
            previous.get(row["case_id"], {}).get("metrics", {}),
        )
    return {
        "baseline_run_id": baseline["run_id"],
        "tolerance": tolerance,
        "regressed": any(d["regression"] for d in differences),
        "differences": differences,
    }


def baseline_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    """Compact CI policy: all overall fields, category/case quality and health."""

    def selected(metrics: dict[str, Any]) -> dict[str, Any]:
        return {
            name: value
            for name, value in metrics.items()
            if name.startswith("refine.")
            and not name.endswith("selected_characters")
            or name.endswith((".exact", ".success"))
            or name == "revise.lines"
        }

    return {
        **{
            key: report[key]
            for key in (
                "report_version",
                "scoring_version",
                "dataset_hash",
                "dataset_versions",
                "run_id",
                "started_at",
                "backend",
                "models",
                "fixture_hash",
                "prompts",
                "prompt_hashes",
                "implementation_hash",
                "sample_count",
            )
        },
        "metrics": report["metrics"],
        "categories": {
            name: {"metrics": selected(category["metrics"])}
            for name, category in report["categories"].items()
        },
        "cases": [
            {"case_id": row["case_id"], "metrics": selected(row["metrics"])}
            for row in report["cases"]
        ],
    }


def summary(report: dict[str, Any]) -> str:
    lines = [
        f"Evaluation: {report['sample_count']} cases, {report['backend']} ({', '.join(report['models'])})"
    ]
    for name in (
        "refine.context_recall",
        "refine.trigger_recall",
        "extract.exact",
        "pipeline.exact",
        "raw_pipeline.exact",
        "revise.lines",
    ):
        if value := report["metrics"].get(name):
            score = "n/a" if value["score"] is None else f"{value['score']:.1%}"
            lines.append(
                f"{name}: {value['numerator']}/{value['denominator']} ({score})"
            )
    lines.append(f"Stage errors: {sum(len(r['errors']) for r in report['cases'])}")
    if comparison := report.get("comparison"):
        lines.append(
            "Baseline: REGRESSION" if comparison["regressed"] else "Baseline: passed"
        )
        for difference in [d for d in comparison["differences"] if d["regression"]][:5]:
            lines.append(
                f"  {difference['scope']} {difference['metric']}: delta={difference['delta']}"
            )
    return "\n".join(lines)
