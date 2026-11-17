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
