"""Runtime integration of v2 review and summary lookup."""

import threading

from weaver.config import load_config
from weaver.courier.courier_v2 import CourierV2
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


def test_runtime_uses_v2_transport(tmp_path):
    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    config = load_config(config_file, init_directories=True)
    runtime = build_runtime(config, plugins=Plugins(client=FakeLlm()))
    assert isinstance(runtime.scanner, CourierV2)
    assert runtime._tick(threading.Event()) == runtime.interval


def test_capture_approval_and_restart(tmp_path):
    import hashlib
    import json

    from weaver.fabric.v2 import FabricStore, ThreadQuery
    from weaver.schema.v2 import ReviewPackage

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
    from weaver.schema.v2 import EventSummary, ReviewPackage

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
