import json
from pathlib import Path

from typer.testing import CliRunner

from weaver.evals.__main__ import app


def test_summary_replay_scores_draft_questions_and_threads(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    result = CliRunner().invoke(app, ["summary", "--output", str(output)])
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["cases"][0]["metrics"] == {
        "draft.exact": True,
        "questions.exact": True,
        "threads.exact": True,
    }
    assert [call["task"] for call in report["cases"][0]["calls"]] == [
        "loom.summary",
        "loom.approve",
    ]


def test_summary_replay_reports_mismatch(tmp_path: Path) -> None:
    from weaver.evals.client import ReplayClient
    from weaver.evals.summary_runner import SUMMARY_CASES, evaluate_summary

    case = json.loads(SUMMARY_CASES.read_text())["cases"][0]
    case["expected"]["body"] = "다른 요약"
    row = evaluate_summary(case, ReplayClient(case["responses"]))
    assert not row["metrics"]["draft.exact"]
    assert row["metrics"]["threads.exact"]


def test_summary_eval_passes_case_source_time_to_approval():
    from weaver.evals.client import ReplayClient
    from weaver.evals.summary_runner import SUMMARY_CASES, evaluate_summary

    class RecordingClient(ReplayClient):
        approval = None

        def generate_json(self, task, system, user, schema):
            if task == "loom.approve":
                self.approval = json.loads(user)
            return super().generate_json(task, system, user, schema)

    case = json.loads(SUMMARY_CASES.read_text())["cases"][0]
    case["source_occurred_at"] = "2026-11-11T16:30:00Z"
    client = RecordingClient(case["responses"])
    evaluate_summary(case, client)
    assert client.approval["source_occurred_at"] == "2026-11-12T01:30:00+09:00"
    assert client.approval["source_time_is_fallback"] is False
