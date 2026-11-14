"""Runtime integration of review, summary lookup, schedules and alerts."""

import threading
from pathlib import Path

from weaver.config import load_config
from weaver.courier import Courier
from weaver.runtime.assemble import Plugins, build_runtime


class FakeLlm:
    model = "fake"

    def generate_json(self, task, system, user, schema):
        if task == "loom.summary":
            return schema.model_validate(
                {"script": "원문", "body": "요약", "questions": []}
            )
        if task == "loom.approve":
            return schema.model_validate(
                {"body": "요약", "corrections": [], "threads": []}
            )
        raise AssertionError(task)

    def generate_text(self, task, system, user):
        raise AssertionError(task)


def test_runtime_uses_courier_review_transport(tmp_path):
    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    config = load_config(config_file, init_directories=True)
    runtime = build_runtime(config, plugins=Plugins(client=FakeLlm()))
    assert isinstance(runtime.scanner, Courier)
    assert runtime._tick(threading.Event()) == runtime.interval


def test_capture_approval_and_restart(tmp_path):
    import hashlib
    import json

    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.schema import ReviewPackage

    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    config = load_config(config_file, init_directories=True)
    runtime = build_runtime(config, plugins=Plugins(client=FakeLlm()))
    cap = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    data = b"test text"
    manifest = {
        "schema_version": 1,
        "capture_id": cap,
        "kind": "text",
        "channel": "share_text",
        "captured_at": "2026-01-01T00:00:00Z",
        "occurred_at": None,
        "payload": cap + ".txt",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "hints": {"counterpart": "홍길동"},
    }
    (config.courier.outbox / manifest["payload"]).write_bytes(data)
    (config.courier.outbox / (cap + ".json")).write_text(json.dumps(manifest))
    runtime._tick(threading.Event())
    paths = list(config.courier.proposals.glob("*.json"))
    assert len(paths) == 1
    package = ReviewPackage.model_validate_json(paths[0].read_bytes())
    verdict = {
        "schema_version": 2,
        "package_id": package.package_id,
        "revision": 1,
        "action": "approve",
        "answers": [],
        "edited_body": None,
        "feedback": None,
    }
    (config.courier.decisions / paths[0].name).write_text(json.dumps(verdict))
    runtime = build_runtime(config, plugins=Plugins(client=FakeLlm()))
    runtime._tick(threading.Event())
    assert not list(config.courier.decisions.glob("*.json"))
    assert len(list((config.archive_root / "summaries").glob("*/*.json"))) == 1
    assert FabricStore.from_config(config).entries(ThreadQuery()) == []


def test_review_budget_gate_stops_repeat_calls(tmp_path):
    import json

    from weaver.llm.errors import BudgetExceeded

    class BudgetClient(FakeLlm):
        calls = 0
        exhausted = True

        def generate_json(self, task, system, user, schema):
            if task == "loom.approve":
                self.calls += 1
                if self.exhausted:
                    raise BudgetExceeded()
            return super().generate_json(task, system, user, schema)

    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    config = load_config(config_file, init_directories=True)
    client = BudgetClient()
    runtime = build_runtime(config, plugins=Plugins(client=client))
    now = [1000.0]
    runtime.clock = lambda: now[0]
    runtime.scanner._clock = lambda: now[0]
    package, _ = runtime.processor.drafts.create(
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "원문"
    )
    decision = config.courier.decisions / f"{package.package_id}.r1.json"
    decision.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "package_id": package.package_id,
                "revision": 1,
                "action": "approve",
                "answers": [],
                "edited_body": None,
                "feedback": None,
            }
        )
    )
    runtime._tick(threading.Event())
    assert not runtime.gate.available and client.calls == 1 and decision.exists()
    for _ in range(10):
        runtime._tick(threading.Event())
    assert client.calls == 1 and decision.exists()
    client.exhausted = False
    now[0] += 61
    runtime._tick(threading.Event())
    assert client.calls == 2 and not decision.exists()


def test_replacement_and_interrupted_review_replay(tmp_path, monkeypatch):
    import hashlib
    import json

    import weaver.loom.summary_review as review_module
    from weaver.schema import EventSummary, ReviewPackage

    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    config = load_config(config_file, init_directories=True)
    runtime = build_runtime(config, plugins=Plugins(client=FakeLlm()))
    cap = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    data = b"test text"
    manifest = {
        "schema_version": 1,
        "capture_id": cap,
        "kind": "text",
        "channel": "share_text",
        "captured_at": "2026-01-01T00:00:00Z",
        "occurred_at": None,
        "payload": cap + ".txt",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "hints": {"counterpart": None},
    }
    (config.courier.outbox / manifest["payload"]).write_bytes(data)
    (config.courier.outbox / (cap + ".json")).write_text(json.dumps(manifest))
    runtime._tick(threading.Event())
    path = next(config.courier.proposals.glob("*.json"))
    first = ReviewPackage.model_validate_json(path.read_bytes())

    def decide(package):
        verdict = {
            "schema_version": 2,
            "package_id": package.package_id,
            "revision": package.revision,
            "action": "approve",
            "answers": [],
            "edited_body": None,
            "feedback": None,
        }
        (
            config.courier.decisions / f"{package.package_id}.r{package.revision}.json"
        ).write_text(json.dumps(verdict))

    decide(first)
    runtime._tick(threading.Event())
    summaries = list((config.archive_root / "summaries").glob("*/*.json"))
    original = EventSummary.model_validate_json(summaries[0].read_bytes())
    second, _ = runtime.processor.drafts.create(first.source_id, "수정된 원문")
    decide(second)
    write = review_module.write_json_atomic

    def fail_publication(path, value):
        if path.parent == config.courier.proposals:
            raise OSError("interrupted")
        return write(path, value)

    monkeypatch.setattr(review_module, "write_json_atomic", fail_publication)
    runtime._tick(threading.Event())
    assert len(list(config.courier.decisions.glob("*.json"))) == 1
    monkeypatch.setattr(review_module, "write_json_atomic", write)
    runtime = build_runtime(config, plugins=Plugins(client=FakeLlm()))
    runtime._tick(threading.Event())
    summaries = [
        EventSummary.model_validate_json(p.read_bytes())
        for p in (config.archive_root / "summaries").glob("*/*.json")
    ]
    assert len(summaries) == 2
    replacement = next(
        item for item in summaries if item.summary_id != original.summary_id
    )
    assert replacement.replaces == original.summary_id
    assert not list(config.courier.decisions.glob("*.json"))
    runtime._tick(threading.Event())
    assert len(list((config.archive_root / "summaries").glob("*/*.json"))) == 2


def test_wiki_configuration_and_runtime_wiring(tmp_path):
    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
        + '\n[wiki]\nenabled = true\nfolder = "Notes/Wiki"\ntimezone = "UTC"\nsettle_seconds = 0\ninterval_seconds = 1\n'
    )
    config = load_config(config_file, init_directories=True)
    runtime = build_runtime(config, plugins=Plugins(client=FakeLlm()))
    assert config.wiki.enabled
    assert config.wiki.timezone == "UTC"
    assert any(task.name == "wiki" for task in runtime.maintenance)
    runtime._tick(threading.Event())
    assert (config.vault_root / "Notes/Wiki/열린 요청.md").exists()


def test_capture_to_wiki_and_human_feedback(tmp_path, monkeypatch):
    import hashlib
    import json
    import os
    import time
    from datetime import UTC, datetime

    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.schema import ReviewPackage

    now = [time.time()]

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now[0], tz or UTC)

    monkeypatch.setattr("weaver.wiki.compiler.datetime", ClockDatetime)
    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    with config_file.open("a") as stream:
        stream.write("\n[wiki]\nenabled = true\ninterval_seconds = 1\n")

    class WikiFake(FakeLlm):
        def __init__(self):
            self.compile_inputs = []

        def generate_json(self, task, system, user, schema):
            if task == "loom.approve":
                return schema.model_validate(
                    {
                        "body": "요약",
                        "corrections": [],
                        "threads": [
                            {
                                "kind": "요청",
                                "content": "검토 요청",
                                "occurred_at": "2026-01-01T00:00:00Z",
                                "tags": ["홍길동"],
                                "field": None,
                                "value": None,
                                "direction": "받음",
                                "resolves": None,
                                "retracts": None,
                            }
                        ],
                    }
                )
            if task == "wiki.compile_batch":
                payload = json.loads(user)
                self.compile_inputs.append(payload)
                previous = "\n".join(p["body"] for p in payload["existing_pages"])
                return schema.model_validate(
                    {
                        "pages": [
                            {
                                "title": "홍길동",
                                "role": "person",
                                "summary": "홍길동 관련 업무",
                                "thread_ids": [
                                    t["thread_id"] for t in payload["threads"]
                                ],
                                "state_tag": None,
                                "aliases": [],
                                "body": "# 홍길동\n\n위키 본문\n"
                                + (
                                    "사람의 수정\n" if "사람의 수정" in previous else ""
                                ),
                            }
                        ],
                        "excluded_threads": [],
                    }
                )
            if task == "wiki.digest":
                return schema.model_validate(
                    {
                        "sentences": [
                            {
                                "text": "요약",
                                "evidence": [json.loads(user)["evidence"][0]],
                            }
                        ]
                    }
                )
            if task == "wiki.human_edit":
                return schema.model_validate(
                    {"changes": [{"content": "사람의 수정", "tags": ["홍길동"]}]}
                )
            return super().generate_json(task, system, user, schema)

        def generate_text(self, task, system, user):
            assert task == "wiki.compile"
            payload = json.loads(user)
            self.compile_inputs.append(payload)
            return payload["page"] or "위키 본문"

    config = load_config(config_file, init_directories=True)
    client = WikiFake()
    runtime = build_runtime(config, plugins=Plugins(client=client))
    cap = "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    data = b"test text"
    manifest = {
        "schema_version": 1,
        "capture_id": cap,
        "kind": "text",
        "channel": "share_text",
        "captured_at": "2026-01-01T00:00:00Z",
        "occurred_at": None,
        "payload": cap + ".txt",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "hints": {"counterpart": "홍길동"},
    }
    (config.courier.outbox / manifest["payload"]).write_bytes(data)
    (config.courier.outbox / (cap + ".json")).write_text(json.dumps(manifest))
    runtime._tick(threading.Event())
    paths = list(config.courier.proposals.glob("*.json"))
    assert len(paths) == 1
    package = ReviewPackage.model_validate_json(paths[0].read_bytes())
    verdict = {
        "schema_version": 2,
        "package_id": package.package_id,
        "revision": 1,
        "action": "approve",
        "answers": [],
        "edited_body": None,
        "feedback": None,
    }
    (config.courier.decisions / paths[0].name).write_text(json.dumps(verdict))
    runtime = build_runtime(config, plugins=Plugins(client=client))
    runtime._tick(threading.Event())
    assert not list(config.courier.decisions.glob("*.json"))
    assert len(list((config.archive_root / "summaries").glob("*/*.json"))) == 1
    fabric = FabricStore.from_config(config)
    assert len(fabric.entries(ThreadQuery())) == 1
    root = config.vault_root / "Weaver"
    copy = next((root / "summaries").glob("*.md"))
    assert "요약" in copy.read_text()
    job = next(task.run.__self__ for task in runtime.maintenance if task.name == "wiki")
    job.store.clock = lambda: now[0]
    now[0] += 1801
    job.run()
    assert copy.stem in (root / "열린 요청.md").read_text()
    assert list((root / "Digest").rglob("*.md"))
    page = root / "pages/홍길동.md"
    page.write_text(page.read_text() + "\n사람의 수정\n")
    job.run()
    assert len(fabric.entries(ThreadQuery())) == 1
    assert "사람의 수정" in page.read_text()
    os.utime(page, (now[0], now[0]))
    now[0] += 1801
    job.run()
    threads = fabric.entries(ThreadQuery())
    assert len(threads) == 2 and threads[-1].actor == "human"
    assert "사람의 수정" in client.compile_inputs[-1]["existing_pages"][0]["body"]
    assert "사람의 수정" in page.read_text()
    now[0] += 1801
    job.run()
    assert len(fabric.entries(ThreadQuery())) == 2


def test_wiki_config_rejects_invalid_values(tmp_path):
    import pytest

    from weaver.config import ConfigError

    config_file = tmp_path / "server.toml"
    roots = "\n".join(
        f'{key}_root = "{key}"' for key in ("sync", "archive", "vault", "state", "log")
    )
    config_file.write_text(roots)
    assert not load_config(config_file, init_directories=True).wiki.enabled
    for value in (
        "enabled = 1",
        'folder = "../escape"',
        'timezone = "invalid"',
        "settle_seconds = -1",
        "interval_seconds = 0",
        'vault_root = "state"',
    ):
        config_file.write_text(roots + "\n[wiki]\n" + value)
        with pytest.raises(ConfigError):
            load_config(config_file, init_directories=True)


def test_runtime_publishes_schedule_and_recomputes_after_resolution(tmp_path):
    import json
    from datetime import UTC, datetime

    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.schema import ApprovedSchedule, ThreadDraft

    class ScheduleLlm(FakeLlm):
        def generate_json(self, task, system, user, schema):
            if task == "loom.approve":
                assert json.loads(user)["timezone"] == "America/New_York"
                base = {
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "tags": [],
                    "resolves": None,
                    "retracts": None,
                }
                return schema.model_validate(
                    {
                        "body": "요약",
                        "corrections": [],
                        "threads": [
                            base
                            | {
                                "kind": "요청",
                                "content": "준비",
                                "field": None,
                                "value": None,
                                "direction": "받음",
                            },
                            base
                            | {
                                "kind": "일정",
                                "content": "회의",
                                "field": "schedule",
                                "value": {
                                    "start": "2026-01-02T09:00:00",
                                    "end": None,
                                    "timezone": None,
                                },
                                "direction": None,
                                "blocked_by_index": 0,
                            },
                        ],
                    }
                )
            return super().generate_json(task, system, user, schema)

    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
        + '\n[time]\ntimezone = "America/New_York"\n'
    )
    config = load_config(config_file, init_directories=True)
    runtime = build_runtime(config, plugins=Plugins(client=ScheduleLlm()))
    package, _ = runtime.processor.drafts.create(
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "원문"
    )
    (config.courier.decisions / f"{package.package_id}.r1.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "package_id": package.package_id,
                "revision": 1,
                "action": "approve",
                "answers": [],
                "edited_body": None,
                "feedback": None,
            }
        )
    )
    runtime._tick(threading.Event())
    fabric = FabricStore.from_config(config)
    request, schedule = fabric.entries(ThreadQuery())
    path = config.courier.proposals / "schedules" / f"{schedule.thread_id}.json"
    result = ApprovedSchedule.model_validate_json(path.read_text())
    assert (result.package_id, result.blocked_by, result.timezone, result.revision) == (
        package.package_id,
        request.thread_id,
        "America/New_York",
        1,
    )
    assert (
        config.archive_root
        / "summaries"
        / result.summary_id[:2]
        / f"{result.summary_id}.json"
    ).exists()
    runtime._tick(threading.Event())
    assert ApprovedSchedule.model_validate_json(path.read_text()).revision == 1
    now = datetime.now(UTC)
    fabric.append(
        [
            ThreadDraft(
                thread_id="decision",
                kind="결정",
                content="완료",
                occurred_at=now,
                recorded_at=now,
                tags=[],
                field=None,
                value=None,
                source_id=package.source_id,
                actor="human",
                direction=None,
                resolves=request.thread_id,
                retracts=None,
                correction=None,
            )
        ]
    )
    runtime._tick(threading.Event())
    result = ApprovedSchedule.model_validate_json(path.read_text())
    assert result.blocked_by is None and result.revision == 2
    fabric.append(
        [
            ThreadDraft(
                thread_id="withdrawal",
                kind="기록",
                content="취소",
                occurred_at=now,
                recorded_at=now,
                tags=[],
                field=None,
                value=None,
                source_id=package.source_id,
                actor="human",
                direction=None,
                resolves=None,
                retracts=schedule.thread_id,
                correction=None,
            )
        ]
    )
    runtime = build_runtime(config, plugins=Plugins(client=ScheduleLlm()))
    runtime._tick(threading.Event())
    result = ApprovedSchedule.model_validate_json(path.read_text())
    assert result.retracted and result.revision == 3


def test_runtime_alerts_when_a_new_approval_moves_an_approved_schedule(tmp_path):
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from jsonschema import Draft202012Validator, FormatChecker

    from weaver.alert import AlertHistory, ScheduleAlertDetector
    from weaver.schema import Alert

    class MovingLlm(FakeLlm):
        start = "2026-01-02T09:00:00"

        def generate_json(self, task, system, user, schema):
            if task != "loom.approve":
                return super().generate_json(task, system, user, schema)
            return schema.model_validate(
                {
                    "body": "요약",
                    "corrections": [],
                    "threads": [
                        {
                            "kind": "일정",
                            "content": "주간 회의",
                            "occurred_at": "2026-01-01T00:00:00Z",
                            "tags": ["홍길동"],
                            "field": "schedule",
                            "value": {
                                "start": self.start,
                                "end": None,
                                "timezone": "Asia/Seoul",
                            },
                            "direction": None,
                            "resolves": None,
                            "retracts": None,
                        }
                    ],
                }
            )

    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    config = load_config(config_file, init_directories=True)
    llm = MovingLlm()
    runtime = build_runtime(config, plugins=Plugins(client=llm))
    zone = ZoneInfo("Asia/Seoul")
    detector = ScheduleAlertDetector(
        AlertHistory(config.state_root / "alerts.sqlite3"),
        timezone=zone,
        clock=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=zone),
    )
    runtime.publisher.alerts = (detector, runtime.publisher.alerts[1])

    def approve(source, start):
        llm.start = start
        package, _ = runtime.processor.drafts.create(source, "원문")
        (config.courier.decisions / f"{package.package_id}.r1.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "package_id": package.package_id,
                    "revision": 1,
                    "action": "approve",
                    "answers": [],
                    "edited_body": None,
                    "feedback": None,
                }
            )
        )
        runtime._tick(threading.Event())
        return package

    def alerts():
        return sorted(config.courier.proposals.glob("alert_*.json"))

    approve("src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "2026-01-02T09:00:00")
    assert alerts() == []
    package = approve("src_01ARZ3NDEKTSV4RRFFQ69G5FAW", "2026-01-02T11:00:00")
    [path] = alerts()
    payload = json.loads(path.read_text())
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "schema" / "Alert.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    assert Alert.model_validate(payload).package_id == package.package_id
    assert path.name == f"alert_{payload['alert_id']}.json"
    assert "주간 회의" not in payload["message"] and "홍길동" not in payload["message"]
    # The phone dismissed it: later ticks do not recreate the file.
    path.unlink()
    runtime._tick(threading.Event())
    assert alerts() == []
