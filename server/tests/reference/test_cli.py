import json

from server.tests.reference.conftest import ConfigFactory, Origin, company_toml
from typer.testing import CliRunner

from weaver.app import cli


def test_reference_sync_cli(origin: Origin, make_config: ConfigFactory) -> None:
    origin.commit({"ops/notes.md": "# Notes\n운영 메모 내용\n".encode()})
    _, path = make_config(company_toml(origin))
    runner = CliRunner()
    result = runner.invoke(cli, ["reference", "sync", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.output
    [row] = json.loads(result.output)
    assert row["sphere"] == "회사" and row["full"] is True and row["indexed"] == 1
    found = runner.invoke(
        cli,
        [
            "reference",
            "find",
            "--config",
            str(path),
            "--tag",
            "회사/ops",
            "--query",
            "운영 메모",
        ],
    )
    assert found.exit_code == 0 and "[tag]" in found.output
