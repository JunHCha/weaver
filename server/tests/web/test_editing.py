"""Write contracts tested only with private synthetic state."""

import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.web import create_app

from .test_web import config_at, draft


@pytest.fixture
def state(tmp_path):
    config = config_at(tmp_path)
    FabricStore.from_config(config).append([draft("record", tags=["Alpha", "old"])])
    dictionary = ContextDictionary(config.state_root)
    dictionary.add_entry("Alpha", "project", ("A",))
    dictionary.add_entry("Beta", "person")
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    headers = {
        "X-Weaver-Token": client.get("/api/session").json()["token"],
        "Origin": "http://127.0.0.1",
    }
    return config, client, headers


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://127.0.0.1"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1.evil.example"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Origin": "https://127.0.0.1"},
        {"Origin": "http://user@127.0.0.1"},
        {"Origin": "http://127.0.0.1/path"},
    ],
)
def test_rejects_missing_token_or_foreign_origin(state, headers):
    config, client, valid = state
    supplied = {"X-Weaver-Token": valid["X-Weaver-Token"], **headers}
    if headers == {"Origin": "http://127.0.0.1"}:
        supplied.pop("X-Weaver-Token")
    before = (config.state_root / "fabric.sqlite3").read_bytes()
    response = client.post(
        "/api/threads/record/tags", json={"add": ["A"]}, headers=supplied
    )
    assert response.status_code == 403
    assert (config.state_root / "fabric.sqlite3").read_bytes() == before


def test_token_process_scope_and_referer_fallback(state):
    config, client, headers = state
    assert client.get("/api/session").headers["cache-control"] == "no-store"
    other = TestClient(create_app(config), base_url="http://127.0.0.1")
    assert (
        other.post(
            "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
        ).status_code
        == 403
    )
    headers.pop("Origin")
    headers["Referer"] = "http://127.0.0.1/dictionary?q=Alpha"
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
        ).status_code
        == 200
    )
    headers["Referer"] = "http://evil.example/"
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
        ).status_code
        == 403
    )


def test_tag_correction_and_usage(state):
    config, client, headers = state
    response = client.post(
        "/api/threads/record/tags",
        headers=headers,
        json={
            "add": ["A", "New"],
            "remove": ["old"],
            "kinds": {"New": "topic"},
        },
    )
    assert response.status_code == 200
    correction = response.json()
    assert correction["kind"] == "태그정정"
    assert correction["actor"] == "human"
    assert correction["source_id"] == draft("record").source_id
    assert correction["tags"] == []
    assert correction["correction"] == {
        "target": "record",
        "add": ["Alpha", "New"],
        "remove": ["old"],
    }
    rows = FabricStore.from_config(config).entries(ThreadQuery())
    assert len(rows) == 2
    assert rows[0].tags == ["Alpha", "New"]
    entry = ContextDictionary(config.state_root).resolve("New")
    assert entry and entry.human_edited and entry.kind == "topic"
    entries = {e["name"]: e for e in client.get("/api/dictionary").json()}
    assert entries["Alpha"]["usage_count"] == 1
    assert entries["New"]["usage_count"] == 1
    assert entries["Beta"]["usage_count"] == 0


@pytest.mark.parametrize(
    "edit",
    [
        {},
        {"add": [""]},
        {"add": ["Unknown"]},
        {"add": ["New"], "kinds": {"New": "invalid"}},
        {"add": ["A"], "remove": ["Alpha"]},
    ],
)
def test_invalid_tags_do_not_append(state, edit):
    config, client, headers = state
    assert (
        client.post("/api/threads/record/tags", headers=headers, json=edit).status_code
        == 422
    )
    assert len(FabricStore.from_config(config).entries(ThreadQuery())) == 1
    assert (
        client.post(
            "/api/threads/missing/tags", headers=headers, json={"add": ["A"]}
        ).status_code
        == 404
    )


def test_all_dictionary_edits_and_effective_names(state):
    config, client, headers = state
    assert (
        client.patch(
            "/api/dictionary/Alpha",
            headers=headers,
            json={"name": "Renamed", "kind": "organization"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/dictionary/Renamed/aliases", headers=headers, json={"alias": "alias"}
        ).status_code
        == 200
    )
    assert (
        client.request(
            "DELETE",
            "/api/dictionary/Renamed/aliases",
            headers=headers,
            json={"alias": "alias"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/dictionary/Renamed/merge", headers=headers, json={"target": "Beta"}
        ).status_code
        == 200
    )
    entry = ContextDictionary(config.state_root).resolve("Alpha")
    assert (
        entry and entry.name == "Beta" and entry.kind == "person" and entry.human_edited
    )
    assert client.get("/api/threads").json()[0]["tags"] == ["Beta", "old"]
    assert client.get("/api/dictionary").json()[0]["usage_count"] == 1
    # Removing the displayed canonical name removes historical spellings too.
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"remove": ["Beta"]}
        ).status_code
        == 200
    )
    assert client.get("/api/threads").json()[0]["tags"] == ["old"]
    assert "Beta" not in client.get("/api/states").json()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/api/dictionary/Alpha", {"name": "Beta"}),
        ("patch", "/api/dictionary/missing", {"kind": "topic"}),
        ("post", "/api/dictionary/Alpha/aliases", {"alias": "Beta"}),
        ("delete", "/api/dictionary/missing/aliases", {"alias": "A"}),
        ("post", "/api/dictionary/Alpha/merge", {"target": "missing"}),
    ],
)
def test_dictionary_errors_and_csrf_on_every_endpoint(state, method, path, body):
    _, client, headers = state
    assert client.request(method, path, json=body).status_code == 403
    assert client.request(method, path, headers=headers, json=body).status_code == 422


@pytest.mark.parametrize(
    "endpoint", ["session", "dictionary", "threads", "tags", "states"]
)
def test_gets_remain_readonly_under_daemon_writer(state, endpoint):
    config, client, _ = state
    paths = [config.state_root / name for name in ("fabric.sqlite3", "context.sqlite3")]
    before = [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in paths]
    with sqlite3.connect(paths[0]) as fabric, sqlite3.connect(paths[1]) as dictionary:
        fabric.execute("BEGIN IMMEDIATE")
        dictionary.execute("BEGIN IMMEDIATE")
        assert client.get("/api/" + endpoint).status_code == 200
    assert before == [
        (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in paths
    ]


@pytest.mark.parametrize(
    "store",
    [
        "weaver.web.editing.FabricStore.append",
        "weaver.web.editing.ContextDictionary.add_alias",
    ],
)
def test_locked_write_has_retry_hint(state, store):
    _, client, headers = state
    with patch(store, side_effect=sqlite3.OperationalError("database is locked")):
        if store.endswith("append"):
            response = client.post(
                "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
            )
        else:
            response = client.post(
                "/api/dictionary/Alpha/aliases", headers=headers, json={"alias": "X"}
            )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


def test_empty_reads_create_no_state(tmp_path):
    config = config_at(tmp_path)
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    assert client.get("/api/dictionary").json() == []
    assert client.get("/api/session").status_code == 200
    assert list(config.state_root.iterdir()) == []


def test_development_proxy_keeps_origin_and_token_checks(state):
    _, client, headers = state
    headers |= {"Origin": "http://localhost:3000", "X-Forwarded-Host": "localhost:3000"}
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
        ).status_code
        == 200
    )
    headers |= {"Origin": "http://evil.example", "X-Forwarded-Host": "evil.example"}
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
        ).status_code
        == 403
    )
    headers |= {"Origin": "http://localhost:3000", "X-Forwarded-Host": "localhost:3000"}
    headers.pop("X-Weaver-Token")
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("database", ["fabric.sqlite3", "context.sqlite3"])
def test_real_writer_lock_returns_503_without_lost_data(state, database, monkeypatch):
    config, client, headers = state
    connect = sqlite3.connect
    path = config.state_root / database
    before = path.read_bytes()
    with connect(path) as writer:
        writer.execute("BEGIN IMMEDIATE")

        def bounded_connect(*args, **kwargs):
            if kwargs.get("timeout") == 30:
                kwargs["timeout"] = 0.01
            return connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", bounded_connect)
        if database == "fabric.sqlite3":
            response = client.post(
                "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
            )
        else:
            response = client.patch(
                "/api/dictionary/Alpha", headers=headers, json={"name": "Renamed"}
            )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        writer.rollback()
    assert path.read_bytes() == before
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"add": ["A"]}
        ).status_code
        == 200
    )


def test_invalid_overlap_does_not_create_dictionary_entries(state):
    config, client, headers = state
    response = client.post(
        "/api/threads/record/tags",
        headers=headers,
        json={"add": ["New"], "remove": ["New"], "kinds": {"New": "topic"}},
    )
    assert response.status_code == 422
    assert ContextDictionary(config.state_root).resolve("New") is None


def test_usage_counts_effective_active_tags_once(state):
    config, client, headers = state
    FabricStore.from_config(config).append(
        [
            draft("duplicate", tags=["A", "Alpha"]),
            draft("retracted", tags=["Beta"]),
            draft("retraction", tags=["Beta"], retracts="retracted"),
        ]
    )
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"remove": ["Alpha"]}
        ).status_code
        == 200
    )
    entries = {e["name"]: e for e in client.get("/api/dictionary").json()}
    assert entries["Alpha"]["usage_count"] == 1
    assert entries["Beta"]["usage_count"] == 0
