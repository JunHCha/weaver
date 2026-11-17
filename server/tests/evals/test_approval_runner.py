import json

from typer.testing import CliRunner

from weaver.evals.__main__ import app


def test_approval_replay_covers_rules_and_bootstrap(tmp_path):
    output = tmp_path / "report.json"
    result = CliRunner().invoke(app, ["approval", "--output", str(output)])
    assert result.exit_code == 0, result.output
    rows = json.loads(output.read_text())["cases"]
    assert len(rows) == 4
    assert all(all(row["metrics"].values()) for row in rows)
    assert rows[-1]["calls"][0]["task"] == "loom.reextract_bootstrap"


def test_approval_eval_passes_case_source_time():
    from weaver.evals.approval_runner import APPROVAL_CASES, evaluate_approval
    from weaver.evals.client import ReplayClient

    class RecordingClient(ReplayClient):
        approval = None

        def generate_json(self, task, system, user, schema):
            self.approval = json.loads(user)
            return super().generate_json(task, system, user, schema)

    case = json.loads(APPROVAL_CASES.read_text())["cases"][0]
    case["source_occurred_at"] = "2026-11-11T16:30:00Z"
    client = RecordingClient(case["responses"])
    evaluate_approval(case, client)
    assert client.approval["source_occurred_at"] == "2026-11-12T01:30:00+09:00"
    assert client.approval["source_time_is_fallback"] is False
