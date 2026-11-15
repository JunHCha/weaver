"""All HTTP tests use synthetic databases in temporary directories."""

import sqlite3
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from weaver.app import cli
from weaver.config import load_config
from weaver.context import ContextDictionary, Page
from weaver.fabric import FabricStore
from weaver.runtime.ledger import RuntimeLedger
from weaver.schema import PipelineItem, ThreadDraft
from weaver.web import create_app

SID = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "src_01ARZ3NDEKTSV4RRFFQ69G5FAW"
NOW = datetime(2026, 11, 15, tzinfo=UTC)


def config_at(root):
    path = root / "config.toml"
    path.write_text(
        "\n".join(
            f'{name}_root = "{name}"'
            for name in ("sync", "archive", "vault", "state", "log")
        )
    )
    return load_config(path, init_directories=True)


def draft(key, **changes):
    data = dict(
        thread_id=key,
        kind="기록",
        content="합성 기록",
        occurred_at=NOW,
        recorded_at=NOW,
        tags=["프로젝트", "미정"],
        field=None,
        value=None,
        source_id=SID,
        actor="human",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )
    return ThreadDraft.model_validate(data | changes)


def observation(config, **changes):
    data = dict(
        schema_version=2,
        revision=1,
        capture_id="cap_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        source_id=SID,
        package_id=None,
        package_revision=None,
        source_type="call",
        title="NEVER RETURN SOURCE TITLE OR TEXT",
        location="loom",
        status="review",
        apply_failure_limit=5,
        stage_times={"수집": NOW},
        history=["spinner", "loom"],
        threads=[],
        pages=[],
        digest=None,
        updated_at=NOW,
    )
    item = PipelineItem.model_validate(data | changes)
    with sqlite3.connect(config.state_root / "pipeline.sqlite3") as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS items(source_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR REPLACE INTO items VALUES(?,?)",
            (item.source_id, item.model_dump_json()),
        )


@pytest.fixture
def setup(tmp_path):
    config = config_at(tmp_path)
    store = FabricStore.from_config(config)
    store.append(
        [
            draft(
                "request",
                kind="요청",
                direction="받음",
                field="deadline",
                value="2026-11-20",
            ),
            draft("record", field="status", value="진행중"),
            draft(
                "correct",
                kind="태그정정",
                correction={"target": "record", "add": ["새 태그"], "remove": ["미정"]},
            ),
            draft(
                "execution",
                kind="실행",
                actor="system",
                executes="record",
                tags=[],
                value={
                    "action_id": "act_00000000000000000000000000000000",
                    "action": "calendar.register",
                    "outcome": "completed",
                    "external_id": "synthetic",
                    "error": None,
                },
                content="calendar.register completed",
            ),
            draft("assignee", field="assignees", value={"add": ["가"], "remove": []}),
        ]
    )
    ContextDictionary(config.state_root).sync_from_pages([Page("프로젝트", "project")])
    return config, TestClient(create_app(config))


def test_threads_tags_states(setup):
    config, client = setup
    rows = client.get("/api/threads").json()
    assert len(rows) == 4
    assert "새 태그" in next(t for t in rows if t["thread_id"] == "record")["tags"]
    assert len(client.get("/api/threads?include_executions=true").json()) == 5
    tags = {t["name"]: t["kind"] for t in client.get("/api/tags").json()}
    assert tags == {"프로젝트": "project", "미정": None, "새 태그": None}
    states = client.get("/api/states?tag=프로젝트&tag=미정").json()
    assert set(states) == {"프로젝트", "미정"}
    assert states["프로젝트"] == {
        "deadline": "2026-11-20",
        "schedule": None,
        "status": "진행중",
        "assignees": ["가"],
        "open_requests": ["request"],
    }
    FabricStore.from_config(config).append(
        [draft("resolved", kind="결정", resolves="request")]
    )
    assert (
        client.get("/api/states?tag=프로젝트").json()["프로젝트"]["open_requests"] == []
    )
    assert "새 태그" in client.get("/api/states").json()


def test_sources_and_status(setup):
    config, client = setup
    observation(config)
    spinner = client.get("/api/sources?stage=spinner").json()
    assert spinner[0]["status"] == "완료"
    assert client.get("/api/sources?stage=loom").json()[0]["status"] == "검토 대기"
    assert "NEVER" not in client.get("/api/sources?stage=loom").text
    assert client.get("/api/status").json()["loom"]["waiting_review"] == 1
    ledger = RuntimeLedger(config.state_root / "runtime.sqlite3")
    ledger.record(SID, "retry")
    assert client.get("/api/sources?stage=loom").json()[0]["status"] == "재시도 대기"
    ledger.record(SID, "failed")
    assert client.get("/api/sources?stage=loom").json()[0]["status"] == "중지됨"
    assert client.get("/api/sources?stage=bad").status_code == 422


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("working", "처리 중"),
        ("applying", "처리 중"),
        ("done", "완료"),
        ("rejected", "완료"),
        ("apply_stopped", "중지됨"),
    ],
)
def test_source_statuses(setup, status, expected):
    config, client = setup
    observation(config, status=status)
    assert client.get("/api/sources?stage=loom").json()[0]["status"] == expected


def test_rotated_logs_and_filters(setup):
    config, client = setup
    (config.log_root / "weaver.log.5").write_text(
        f"2026-11-14 09:00:00,123 INFO weaver.spinner.pipeline: queued {SID}\ntraceback junk\n"
    )
    (config.log_root / "weaver.log.1").write_text(
        f"2026-11-15 09:00:00,123 WARNING weaver.spinner: retry {OTHER}\n"
    )
    (config.log_root / "weaver.log").write_text(
        f"2026-11-15 10:00:00,123 INFO weaver.loom.call: complete {SID}\n2026-11-15 11:00:00,123 INFO weaver.spinner: done {SID}\n2026-11-15 12:00:00,123 INFO weaver.spinner_fake: excluded\n"
    )
    assert len(client.get("/api/logs?component=spinner").json()) == 3
    assert (
        client.get("/api/logs?component=spinner&limit=1").json()[0]["msg"]
        == f"done {SID}"
    )
    assert len(client.get(f"/api/logs?component=spinner&source_id={SID}").json()) == 2
    assert (
        client.get("/api/logs?component=spinner&level=WARNING").json()[0]["src"]
        == OTHER
    )
    assert len(client.get("/api/logs?component=loom").json()) == 1
    assert client.get("/api/logs?component=spinner&limit=0").status_code == 422
    assert (
        client.get("/api/status")
        .json()["spinner"]["last_activity"]
        .startswith("2026-11-15T11:00")
    )


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "endpoint", ["threads", "tags", "states", "sources", "logs", "status"]
)
def test_no_write_methods(setup, method, endpoint):
    _, client = setup
    assert getattr(client, method)("/api/" + endpoint).status_code == 405


def test_empty_state_and_static(tmp_path):
    config = config_at(tmp_path)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("FABRIC")
    (web / "spinner.html").write_text("SPINNER")
    (web / "404.html").write_text("NEXT NOT FOUND")
    client = TestClient(create_app(config, web))
    assert client.get("/").text == "FABRIC"
    assert client.get("/spinner").text == "SPINNER"
    assert client.get("/unknown").text == "FABRIC"
    assert client.get("/missing.js").status_code == 404
    assert client.get("/api/missing").status_code == 404
    assert client.get("/api/threads").json() == []
    assert client.get("/api/tags").json() == []
    assert client.get("/api/states").json() == {}
    assert client.get("/api/sources?stage=loom").json() == []
    assert not (config.state_root / "fabric.sqlite3").exists()


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "192.168.1.2", "::", "127.0.0.2", "example.com"]
)
def test_cli_refuses_nonloopback(host):
    result = CliRunner().invoke(cli, ["web", "--host", host])
    assert result.exit_code != 0
    assert "loopback" in result.output


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_cli_loopback_and_static_env(tmp_path, host, monkeypatch):
    config = config_at(tmp_path)
    static = tmp_path / "export"
    static.mkdir()
    monkeypatch.setenv("WEAVER_WEB_DIR", str(static))
    with patch("uvicorn.run") as run:
        result = CliRunner().invoke(
            cli, ["web", "--config", str(config.config_file), "--host", host]
        )
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs == {"host": host, "port": 8765}


def test_log_chunks_preserve_utf8_and_skip_unused_rotations(setup):
    config, client = setup
    message = "합성" * 22000
    (config.log_root / "weaver.log").write_text(
        f"2026-11-15 10:00:00,123 INFO weaver.spinner: {message}\n"
        f"2026-11-15 11:00:00,123 INFO weaver.spinner: latest {SID}"
    )
    with patch(
        "weaver.web._reverse_lines",
        wraps=__import__("weaver.web", fromlist=["_reverse_lines"])._reverse_lines,
    ) as tail:
        rows = client.get("/api/logs?component=spinner&limit=1").json()
        assert len(rows) == 1
        assert tail.call_count == 1
    rows = client.get("/api/logs?component=spinner").json()
    assert rows[0]["msg"] == message
