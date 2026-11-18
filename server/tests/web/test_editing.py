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
        "add": ["New"],
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
            "/api/dictionary",
            headers=headers,
            json={"entry": "Alpha", "name": "Renamed"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/dictionary/aliases",
            headers=headers,
            json={"entry": "Renamed", "alias": "alias"},
        ).status_code
        == 200
    )
    assert (
        client.request(
            "DELETE",
            "/api/dictionary/aliases",
            headers=headers,
            json={"entry": "Renamed", "alias": "alias"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/dictionary/merge",
            headers=headers,
            json={"entry": "Renamed", "target": "Beta", "kind": "person"},
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
        ("patch", "/api/dictionary", {"entry": "Alpha", "name": "Beta"}),
        ("patch", "/api/dictionary", {"entry": "Alpha", "kind": "invalid"}),
        ("post", "/api/dictionary/aliases", {"entry": "Alpha", "alias": "Beta"}),
        ("delete", "/api/dictionary/aliases", {"entry": "missing", "alias": "A"}),
        ("post", "/api/dictionary/merge", {"entry": "Alpha", "target": "missing"}),
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
        "weaver.web.editing.FabricStore.web_edit",
        "weaver.web.editing.ContextDictionary.add_alias",
    ],
)
def test_locked_write_has_retry_hint(state, store):
    _, client, headers = state
    with patch(store, side_effect=sqlite3.OperationalError("database is locked")):
        if store.endswith("web_edit"):
            response = client.post(
                "/api/threads/record/tags", headers=headers, json={"add": ["Beta"]}
            )
        else:
            response = client.post(
                "/api/dictionary/aliases",
                headers=headers,
                json={"entry": "Alpha", "alias": "X"},
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
                "/api/threads/record/tags", headers=headers, json={"add": ["Beta"]}
            )
        else:
            response = client.patch(
                "/api/dictionary",
                headers=headers,
                json={"entry": "Alpha", "name": "Renamed"},
            )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        writer.rollback()
    assert path.read_bytes() == before
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, json={"add": ["Beta"]}
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


@pytest.mark.parametrize("edit", [{}, {"add": ["A", "old"], "remove": ["absent"]}])
def test_noop_returns_current_state_without_writes(state, edit):
    config, client, headers = state
    before = {p: p.read_bytes() for p in config.state_root.glob("*.sqlite3")}
    with patch(
        "weaver.web.editing.writable_dictionary",
        side_effect=AssertionError("must not open for writing"),
    ):
        response = client.post("/api/threads/record/tags", headers=headers, json=edit)
    assert response.status_code == 200
    assert response.json()["thread_id"] == "record"
    assert response.json()["tags"] == ["Alpha", "old"]
    assert before == {p: p.read_bytes() for p in before}


def test_noop_after_effective_correction_and_duplicate_requests(state):
    config, client, headers = state
    edit = {"add": ["Beta"], "remove": ["old"]}
    first = client.post("/api/threads/record/tags", headers=headers, json=edit)
    assert first.status_code == 200
    second = client.post("/api/threads/record/tags", headers=headers, json=edit)
    assert second.status_code == 200
    assert second.json()["thread_id"] == "record"
    assert second.json()["tags"] == ["Alpha", "Beta"]
    assert len(FabricStore.from_config(config).entries(ThreadQuery())) == 2


def test_slash_names_in_every_dictionary_write(state):
    config, client, headers = state
    ContextDictionary(config.state_root).add_entry("a/b", "topic")
    assert (
        client.patch(
            "/api/dictionary", headers=headers, json={"entry": "a/b", "name": "c/d"}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            "/api/dictionary", headers=headers, json={"entry": "c/d", "kind": "person"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/dictionary/aliases",
            headers=headers,
            json={"entry": "c/d", "alias": "e/f"},
        ).status_code
        == 200
    )
    assert (
        client.request(
            "DELETE",
            "/api/dictionary/aliases",
            headers=headers,
            json={"entry": "c/d", "alias": "e/f"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/dictionary/merge",
            headers=headers,
            json={"entry": "c/d", "target": "Beta"},
        ).status_code
        == 200
    )
    assert ContextDictionary(config.state_root).resolve("a/b").name == "Beta"


def test_unknown_entry_checked_before_write_and_combined_edit_rejected(state):
    config, client, headers = state
    before = (config.state_root / "context.sqlite3").read_bytes()
    with patch(
        "weaver.web.editing.writable_dictionary",
        side_effect=AssertionError("must not write"),
    ):
        missing = client.patch(
            "/api/dictionary",
            headers=headers,
            json={"entry": "missing", "name": "renamed"},
        )
        assert missing.status_code == 404
        combined = client.patch(
            "/api/dictionary",
            headers=headers,
            json={"entry": "Alpha", "name": "renamed", "kind": "topic"},
        )
        assert combined.status_code == 422
    assert (config.state_root / "context.sqlite3").read_bytes() == before


def test_value_errors_are_fixed_messages_and_only_edit_routes_translate(state):
    config, client, headers = state
    with patch(
        "weaver.web.editing.ContextDictionary.rename",
        side_effect=ValueError("SECRET internal detail"),
    ):
        response = client.patch(
            "/api/dictionary",
            headers=headers,
            json={"entry": "Alpha", "name": "renamed"},
        )
    assert response.status_code == 422
    assert response.json() == {
        "detail": "사전 편집 값을 확인하세요. 이름 또는 별칭이 충돌할 수 있습니다."
    }
    safe_client = TestClient(
        create_app(config), base_url="http://127.0.0.1", raise_server_exceptions=False
    )
    with patch("weaver.web.sources", side_effect=ValueError("SECRET")):
        assert safe_client.get("/api/status").status_code == 500
    with patch(
        "weaver.web.editing.dictionary_snapshot", side_effect=ValueError("SECRET")
    ):
        assert safe_client.get("/api/dictionary").status_code == 500
    with patch(
        "weaver.web.editing.ContextDictionary.rename",
        side_effect=RuntimeError("SECRET"),
    ):
        session = safe_client.get("/api/session").json()["token"]
        response = safe_client.patch(
            "/api/dictionary",
            headers={**headers, "X-Weaver-Token": session},
            json={"entry": "Alpha", "name": "renamed"},
        )
        assert response.status_code == 500


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "POST",
            "/api/threads/record/tags",
            {"add": ["New"], "kinds": {"New": "topic"}},
        ),
        ("PATCH", "/api/dictionary", {"entry": "Alpha", "name": "Renamed"}),
        ("POST", "/api/dictionary/aliases", {"entry": "Alpha", "alias": "alias"}),
        ("DELETE", "/api/dictionary/aliases", {"entry": "Alpha", "alias": "A"}),
        (
            "POST",
            "/api/dictionary/merge",
            {"entry": "Alpha", "target": "Beta", "kind": "person"},
        ),
    ],
)
def test_running_v1_daemon_blocks_upgrade_on_every_write(tmp_path, method, path, body):
    from weaver.runtime.loop import InstanceLock

    config = config_at(tmp_path)
    FabricStore.from_config(config).append([draft("record", tags=["Alpha"])])
    database = config.state_root / "context.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE wiki(name TEXT PRIMARY KEY, kind TEXT, summary TEXT);
            CREATE TABLE wiki_alias(name TEXT, alias TEXT, PRIMARY KEY(name,alias));
            CREATE TABLE learned(name TEXT PRIMARY KEY, kind TEXT);
            CREATE TABLE corrections(observed TEXT PRIMARY KEY, name TEXT);
            INSERT INTO wiki VALUES ('Alpha','project','');
            INSERT INTO wiki VALUES ('Beta','person','');
            INSERT INTO wiki_alias VALUES ('Alpha','A');
            PRAGMA user_version=1;
        """)
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    headers = {
        "Origin": "http://127.0.0.1",
        "X-Weaver-Token": client.get("/api/session").json()["token"],
    }
    before = database.read_bytes()
    with InstanceLock(config.state_root / "weaver.lock"):
        assert client.get("/api/dictionary").status_code == 200
        response = client.request(method, path, headers=headers, json=body)
        assert response.status_code == 503
        assert "데몬을 종료" in response.json()["detail"]
        assert response.headers["retry-after"] == "1"
        assert database.read_bytes() == before
    assert client.request(method, path, headers=headers, json=body).status_code == 200
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.parametrize(
    "body",
    [
        {"add": ["Beta"] * 101},
        {"remove": ["Alpha"] * 101},
        {"add": ["x" * 257]},
        {"remove": ["x" * 257]},
        {"kinds": {str(i): "topic" for i in range(101)}},
    ],
)
def test_tag_caps_reject_before_writing(state, body):
    config, client, headers = state
    before = (config.state_root / "fabric.sqlite3").read_bytes()
    assert (
        client.post("/api/threads/record/tags", headers=headers, json=body).status_code
        == 422
    )
    assert (config.state_root / "fabric.sqlite3").read_bytes() == before


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "/api/dictionary", {"entry": "x" * 257, "kind": "topic"}),
        ("PATCH", "/api/dictionary", {"entry": "Alpha", "name": "x" * 257}),
        ("POST", "/api/dictionary/aliases", {"entry": "Alpha", "alias": "x" * 257}),
        ("POST", "/api/dictionary/merge", {"entry": "Alpha", "target": "x" * 257}),
    ],
)
def test_dictionary_name_caps(state, method, path, body):
    _, client, headers = state
    assert client.request(method, path, headers=headers, json=body).status_code == 422


def test_body_limit_applies_to_streamed_bodies_and_boundary(state):
    _, client, headers = state
    headers = {**headers, "Content-Type": "application/json"}
    assert (
        client.post(
            "/api/threads/record/tags", headers=headers, content=b"{}" + b" " * 65534
        ).status_code
        == 200
    )
    for path in (
        "/api/threads/record/tags",
        "/api/dictionary/aliases",
        "/api/dictionary/merge",
    ):
        response = client.post(
            path, headers=headers, content=iter([b"{}", b" " * 65535])
        )
        assert response.status_code == 413


def test_archived_projects_remain_readable_with_lifecycle_flags(state):
    config, client, _ = state
    dictionary = ContextDictionary(config.state_root)
    dictionary.set_project_lifecycle("Alpha", promoted=True, archived=True)
    before = (config.state_root / "context.sqlite3").read_bytes()
    tag = next(t for t in client.get("/api/tags").json() if t["name"] == "Alpha")
    assert tag == {"name": "Alpha", "kind": "project", "archived": True}
    entry = next(
        e for e in client.get("/api/dictionary").json() if e["name"] == "Alpha"
    )
    assert entry["archived"] and entry["promoted"] and entry["usage_count"] == 1
    assert client.get("/api/threads").json()[0]["tags"] == ["Alpha", "old"]
    assert (config.state_root / "context.sqlite3").read_bytes() == before


def test_undo_tag_correction_appends_retraction_and_preserves_later_edits(state):
    config, client, headers = state
    first = client.post(
        "/api/threads/record/tags",
        headers=headers,
        json={"add": ["Beta"], "remove": ["old"]},
    ).json()
    later = client.post(
        "/api/threads/record/tags",
        headers=headers,
        json={"add": ["New"], "kinds": {"New": "area"}},
    ).json()
    endpoint = f"/api/corrections/{first['thread_id']}/undo"
    assert client.post(endpoint, json={}).status_code == 403
    assert (
        client.post(
            endpoint, headers={**headers, "Origin": "http://evil.example"}, json={}
        ).status_code
        == 403
    )
    response = client.post(endpoint, headers=headers, json={})
    assert response.status_code == 200
    undo = response.json()
    assert undo["retracts"] == first["thread_id"]
    assert undo["actor"] == "human"
    assert client.post(endpoint, headers=headers, json={}).json() == undo
    rows = FabricStore.from_config(config).entries(ThreadQuery())
    assert len(rows) == 4
    assert rows[0].tags == ["Alpha", "old", "New"]
    assert rows[2].thread_id == later["thread_id"]


@pytest.mark.parametrize("target,status", [("missing", 404), ("record", 422)])
def test_undo_rejects_non_corrections(state, target, status):
    _, client, headers = state
    assert (
        client.post(
            f"/api/corrections/{target}/undo", headers=headers, json={}
        ).status_code
        == status
    )


def test_dictionary_last_used_tracks_effective_active_threads(state):
    config, client, headers = state
    from datetime import UTC, datetime

    recent = datetime(2026, 11, 18, tzinfo=UTC)
    FabricStore.from_config(config).append(
        [
            draft("recent", tags=["A"], occurred_at=recent),
            draft("retired", tags=["Beta"], occurred_at=recent),
            draft("retired-undo", tags=[], retracts="retired"),
        ]
    )
    entries = {entry["name"]: entry for entry in client.get("/api/dictionary").json()}
    assert entries["Alpha"]["usage_count"] == 2
    assert entries["Alpha"]["last_used_at"] == recent.isoformat()
    assert entries["Beta"]["last_used_at"] is None
    client.post("/api/threads/recent/tags", headers=headers, json={"remove": ["A"]})
    entries = {entry["name"]: entry for entry in client.get("/api/dictionary").json()}
    assert entries["Alpha"]["usage_count"] == 1
    assert entries["Alpha"]["last_used_at"] == draft("record").occurred_at.isoformat()


def test_batch_atomic_counts_limit_and_unknown_removal(state):
    config, client, headers = state
    store = FabricStore.from_config(config)
    store.append([draft("second", tags=["Beta", "old"])])
    endpoint = "/api/threads/tags/batch"
    body = {"thread_ids": ["record", "second"], "add": ["Beta"], "remove": ["old"]}
    assert client.post(endpoint, json=body).status_code == 403
    assert (
        client.post(
            endpoint, headers={**headers, "Origin": "https://evil.example"}, json=body
        ).status_code
        == 403
    )
    response = client.post(endpoint, headers=headers, json=body)
    assert response.status_code == 200
    assert response.json()["changed"] == 2
    assert response.json()["skipped"] == 0
    again = client.post(endpoint, headers=headers, json=body).json()
    assert again["changed"] == 0 and again["skipped"] == 2
    assert {r["thread_id"] for r in again["skipped_threads"]} == {"record", "second"}
    assert all(r["reason"] for r in again["skipped_threads"])
    before = store.entries(ThreadQuery())
    invalid = {"thread_ids": ["record", "missing"], "remove": ["Beta"]}
    assert client.post(endpoint, headers=headers, json=invalid).status_code == 404
    assert store.entries(ThreadQuery()) == before
    with patch(
        "weaver.fabric.store.FabricStore._validate_references",
        side_effect=ValueError("invalid"),
    ):
        assert (
            client.post(
                endpoint,
                headers=headers,
                json={"thread_ids": ["record", "second"], "remove": ["Beta"]},
            ).status_code
            == 422
        )
    assert store.entries(ThreadQuery()) == before
    assert (
        client.post(
            endpoint,
            headers=headers,
            json={"thread_ids": ["record"] * 501, "remove": ["Beta"]},
        ).status_code
        == 422
    )
    accepted = client.post(
        endpoint,
        headers=headers,
        json={"thread_ids": ["record"] * 500, "remove": ["Beta"]},
    )
    assert accepted.status_code == 200 and accepted.json()["changed"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "거절", "content": None},
        {
            "kind": "태그정정",
            "correction": {"target": "record", "add": [], "remove": []},
        },
        {"retracts": "record"},
        {"thread_id": "wiki-field:test", "field": "status", "value": "진행중"},
        {
            "thread_id": "wiki:old",
            "field": "status",
            "value": "진행중",
            "content": "status 변경",
        },
        {
            "kind": "실행",
            "actor": "system",
            "executes": "record",
            "tags": [],
            "value": {
                "action_id": "act_00000000000000000000000000000000",
                "action": "calendar.register",
                "outcome": "completed",
                "external_id": "synthetic",
                "error": None,
            },
        },
    ],
)
def test_history_rejects_single_and_batch_without_side_effects(state, changes):
    config, client, headers = state
    item = draft("history", **changes)
    store = FabricStore.from_config(config)
    store.append([item])
    before = store.entries(ThreadQuery(include_executions=True))
    edit = {"add": ["NewHistory"], "kinds": {"NewHistory": "topic"}}
    assert (
        client.post(
            f"/api/threads/{item.thread_id}/tags", headers=headers, json=edit
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/threads/tags/batch",
            headers=headers,
            json={"thread_ids": ["record", item.thread_id], **edit},
        ).status_code
        == 422
    )
    assert store.entries(ThreadQuery(include_executions=True)) == before
    assert ContextDictionary(config.state_root).resolve("NewHistory") is None


def test_concurrent_undo_has_one_retraction_and_requires_explicit_provenance(state):
    from concurrent.futures import ThreadPoolExecutor

    from weaver.schema import TagCorrection

    config, client, headers = state
    store = FabricStore.from_config(config)
    store.append(
        [
            draft(
                "foreign",
                kind="태그정정",
                content="웹에서 태그 정정",
                tags=[],
                correction=TagCorrection(target="record", add=["Beta"], remove=[]),
            )
        ]
    )
    assert (
        client.post(
            "/api/corrections/foreign/undo", headers=headers, json={}
        ).status_code
        == 422
    )
    correction = client.post(
        "/api/threads/record/tags", headers=headers, json={"remove": ["old"]}
    ).json()
    url = f"/api/corrections/{correction['thread_id']}/undo"
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: client.post(url, headers=headers, json={}), range(8))
        )
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["thread_id"] for response in responses}) == 1
    rows = store.entries(ThreadQuery())
    assert sum(row.retracts == correction["thread_id"] for row in rows) == 1
    assert ContextDictionary(config.state_root).resolve("Beta") is not None


def test_dismissal_persists_in_dictionary_and_cross_kind_merge_needs_choice(state):
    config, client, headers = state
    endpoint = "/api/dictionary/dismissed"
    assert client.post(endpoint, json={"a": "Alpha", "b": "Other"}).status_code == 403
    assert (
        client.post(
            endpoint, headers=headers, json={"a": "Alpha", "b": "Other"}
        ).status_code
        == 200
    )
    other = TestClient(create_app(config), base_url="http://127.0.0.1")
    assert other.get(endpoint).json() == [["alpha", "other"]]
    merge = {"entry": "Alpha", "target": "Beta"}
    assert (
        client.post("/api/dictionary/merge", headers=headers, json=merge).status_code
        == 422
    )
    assert (
        client.post(
            "/api/dictionary/merge", headers=headers, json={**merge, "kind": "area"}
        ).status_code
        == 200
    )
    entry = ContextDictionary(config.state_root).resolve("Alpha")
    assert (
        entry
        and entry.name == "Beta"
        and entry.kind == "area"
        and "Alpha" in entry.aliases
    )


def test_new_tag_conflict_names_existing_entry_and_preserves_kind(state):
    config, client, headers = state
    response = client.post(
        "/api/threads/record/tags",
        headers=headers,
        json={"add": ["A"], "kinds": {"A": "topic"}},
    )
    assert response.status_code == 422
    assert "Alpha" in response.json()["detail"]
    assert ContextDictionary(config.state_root).resolve("A").kind == "project"
    response = client.post(
        "/api/dictionary/aliases", headers=headers, json={"entry": "Beta", "alias": "A"}
    )
    assert response.status_code == 422 and "Alpha" in response.json()["detail"]


def test_batch_rolls_back_first_insert_when_second_insert_fails(state):
    config, client, headers = state
    store = FabricStore.from_config(config)
    store.append([draft("second", tags=[])])
    before = store.entries(ThreadQuery())
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER synthetic_batch_failure BEFORE INSERT ON threads
            WHEN json_extract(NEW.payload, '$.correction.target') = 'second'
            BEGIN SELECT RAISE(ABORT, 'synthetic second insert failure'); END""")
    safe_client = TestClient(
        client.app, base_url="http://127.0.0.1", raise_server_exceptions=False
    )
    response = safe_client.post(
        "/api/threads/tags/batch",
        headers=headers,
        json={"thread_ids": ["record", "second"], "add": ["Beta"]},
    )
    assert response.status_code == 500
    assert store.entries(ThreadQuery()) == before
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='web_tag_corrections'"
            ).fetchone()[0]
            == 0
        )
