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
    from weaver.evals.runner import ReplayClient
    from weaver.evals.summary_runner import evaluate_summary

    case = json.loads(Path("server/evals/summary_cases.json").read_text())["cases"][0]
    case["expected"]["body"] = "다른 요약"
    row = evaluate_summary(case, ReplayClient(case["responses"]))
    assert not row["metrics"]["draft.exact"]
    assert row["metrics"]["threads.exact"]
