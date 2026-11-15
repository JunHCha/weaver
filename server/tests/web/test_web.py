"""All HTTP tests use synthetic databases in temporary directories."""

import os
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


@pytest.fixture(autouse=True)
def restore_process_umask():
    original = os.umask(0o077)
    os.umask(original)
    try:
        yield
    finally:
        os.umask(original)


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
    return config, TestClient(create_app(config), base_url="http://127.0.0.1")


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
        client.get("/api/status").json()["spinner"]["last_activity"]
        == datetime(2026, 11, 15, 11, 0, 0, 123000)
        .astimezone(config.time.zone)
        .isoformat()
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
    (web / "loom.html").write_text("LOOM")
    (web / "404.html").write_text("NEXT NOT FOUND")
    client = TestClient(create_app(config, web), base_url="http://127.0.0.1")
    assert client.get("/").text == "FABRIC"
    assert client.get("/spinner").text == "SPINNER"
    assert client.get("/spinner/").text == "SPINNER"
    assert client.get("/loom").text == "LOOM"
    assert client.get("/unknown").status_code == 404
    assert client.get("/unknown").text == "NEXT NOT FOUND"
    assert client.get("/missing.js").status_code == 404
    assert client.get("/missing.js").text == "NEXT NOT FOUND"
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


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:8765",
        "localhost",
        "localhost:3000",
        "[::1]",
        "[::1]:8765",
    ],
)
def test_trusted_loopback_hosts(setup, host):
    _, client = setup
    assert client.get("/api/threads", headers={"host": host}).status_code == 200


def test_rejects_dns_rebinding_host(setup):
    _, client = setup
    assert (
        client.get("/api/threads", headers={"host": "evil.example"}).status_code == 400
    )


def test_reads_preserve_database_bytes_mtime_and_mode_during_write_transactions(setup):
    config, client = setup
    # Compare main DB files; normal SQLite -wal/-shm creation is allowed.
    paths = [
        config.state_root / "fabric.sqlite3",
        config.state_root / "context.sqlite3",
    ]
    for path in paths:
        path.chmod(0o640)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        for path in paths
    }
    with (
        sqlite3.connect(paths[0]) as fabric_writer,
        sqlite3.connect(paths[1]) as context_writer,
    ):
        fabric_writer.execute("BEGIN IMMEDIATE")
        context_writer.execute("BEGIN IMMEDIATE")
        for endpoint in ("threads", "tags", "states"):
            assert client.get("/api/" + endpoint).status_code == 200
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        for path in paths
    } == before


@pytest.mark.parametrize(
    ("database", "endpoint"),
    [("fabric.sqlite3", "threads"), ("context.sqlite3", "tags")],
)
def test_unsupported_schema_returns_clear_503_without_migration(
    setup, database, endpoint
):
    config, client = setup
    path = config.state_root / database
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    before = path.read_bytes()
    response = client.get("/api/" + endpoint)
    assert response.status_code == 503
    assert "Unsupported state database schema" in response.json()["detail"]
    assert path.read_bytes() == before


def test_missing_observation_tables_are_empty(setup):
    config, client = setup
    for name in ("runtime.sqlite3", "pipeline.sqlite3"):
        with sqlite3.connect(config.state_root / name):
            pass
    assert client.get("/api/sources?stage=spinner").json() == []
    assert client.get("/api/sources?stage=loom").json() == []
    assert client.get("/api/status").json()["loom"] == {
        "last_activity": None,
        "waiting_review": 0,
    }


def test_invalid_observation_is_skipped_without_logging_content(setup, caplog):
    config, client = setup
    observation(config)
    with sqlite3.connect(config.state_root / "pipeline.sqlite3") as db:
        db.execute(
            "INSERT INTO items VALUES(?,?)",
            ("invalid", '{"private":"SECRET SOURCE TEXT"}'),
        )
    assert len(client.get("/api/sources?stage=loom").json()) == 1
    assert client.get("/api/status").json()["loom"]["waiting_review"] == 1
    assert "invalid pipeline observation skipped" in caplog.text
    assert "SECRET" not in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    "path", ["/..%2fsecret", "/%2e%2e/secret", "/%2e%2e%2fsecret.html"]
)
def test_static_path_traversal_does_not_escape_export(tmp_path, path):
    config = config_at(tmp_path)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("FABRIC")
    (web / "404.html").write_text("NOT FOUND")
    (tmp_path / "secret.html").write_text("DO NOT SERVE")
    client = TestClient(create_app(config, web), base_url="http://127.0.0.1")
    response = client.get(path)
    assert response.status_code == 404
    assert "DO NOT SERVE" not in response.text


def test_status_interprets_log_time_in_system_local_zone(setup, monkeypatch):
    import time

    config, client = setup
    (config.log_root / "weaver.log").write_text(
        "2026-01-15 10:00:00,123 INFO weaver.spinner: synthetic\n"
    )
    try:
        with monkeypatch.context() as environment:
            environment.setenv("TZ", "UTC")
            time.tzset()
            assert (
                client.get("/api/status").json()["spinner"]["last_activity"]
                == "2026-01-15T19:00:00.123000+09:00"
            )
    finally:
        time.tzset()


def test_web_cli_creates_private_files_and_wal_sidecars(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    monkeypatch.delenv("WEAVER_WEB_DIR", raising=False)
    path = config.state_root / "wal.sqlite3"
    # A stopped WAL database may have no sidecars until its next reader opens it.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE sample(value INTEGER)")
        db.execute("INSERT INTO sample VALUES(1)")
    db.close()
    # RuntimeLedger and Fabric create private 0600 main databases.
    path.chmod(0o600)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)

    def serve(*args, **kwargs):
        probe = config.state_root / "new-file"
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT, 0o666)
        os.close(descriptor)
        assert probe.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as reader:
            assert reader.execute("SELECT value FROM sample").fetchone() == (1,)
            for suffix in ("-wal", "-shm"):
                assert (
                    path.with_name(path.name + suffix).stat().st_mode & 0o777 == 0o600
                )

    # Begin permissively to verify the command itself installs the private mask.
    os.umask(0o022)
    with patch("uvicorn.run", side_effect=serve):
        result = CliRunner().invoke(cli, ["web", "--config", str(config.config_file)])
    assert result.exit_code == 0, result.output
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == before
