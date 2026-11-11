"""Pipeline observations are durable, ordered and never expose uncommitted pages."""

from datetime import UTC, datetime

from weaver.runtime.progress import PipelineJournal
from weaver.schema import PipelineItem, Source


def source():
    return Source.model_validate(
        {
            "schema_version": 1,
            "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "capture_id": "cap_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "kind": "text",
            "channel": "share_text",
            "captured_at": "2026-01-01T00:00:00Z",
            "occurred_at": None,
            "sha256": "a" * 64,
            "hints": {"counterpart": None},
            "payload_ref": "payloads/example.txt",
        }
    )


def test_journal_survives_restart_and_publishes_missing_files(tmp_path):
    journal = PipelineJournal(tmp_path / "state", tmp_path / "proposals")
    item = journal.receive(source(), datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
    assert item.stage_times["수집"] == source().captured_at
    assert item.stage_times["전송"].minute == 1
    journal.update(item.source_id, location="loom", status="working")
    journal.update(item.source_id, location="user", status="review")
    seen = journal.get(item.source_id)
    assert seen.history == ["spinner", "loom", "user"]
    assert journal.update(item.source_id, location="user", status="review") == seen
    path = tmp_path / "proposals" / "pipeline" / f"{item.source_id}.json"
    path.unlink()
    restarted = PipelineJournal(tmp_path / "state", tmp_path / "proposals")
    restarted.publish()
    assert PipelineItem.model_validate_json(path.read_bytes()) == seen
    assert restarted.get(item.source_id).revision == seen.revision


def test_feedback_history_and_stopped_do_not_become_done_on_restart(tmp_path):
    journal = PipelineJournal(tmp_path / "state", tmp_path / "proposals")
    sid = journal.receive(source(), datetime.now(UTC)).source_id
    for location, status in [
        ("loom", "working"),
        ("user", "review"),
        ("loom", "applying"),
        ("user", "review"),
        ("loom", "apply_stopped"),
    ]:
        journal.update(sid, location=location, status=status)
    restarted = PipelineJournal(tmp_path / "state", tmp_path / "proposals")
    assert restarted.get(sid).history == [
        "spinner",
        "loom",
        "user",
        "loom",
        "user",
        "loom",
    ]
    assert restarted.get(sid).status == "apply_stopped"


def runtime_capture(tmp_path, failure_limit=5):
    import hashlib
    import json

    from weaver.config import load_config
    from weaver.runtime.assemble import Plugins, build_runtime

    class FakeLlm:
        model = "fake"
        failing = True

        def generate_json(self, task, system, user, schema):
            if task == "loom.summary":
                return schema.model_validate(
                    {"script": "가상 메모", "body": "가상 요약", "questions": []}
                )
            if task == "loom.approve":
                if self.failing:
                    raise RuntimeError("synthetic error")
                return schema.model_validate(
                    {
                        "body": "가상 요약",
                        "corrections": [],
                        "threads": [
                            {
                                "kind": "기록",
                                "content": "가상 기록",
                                "occurred_at": "2026-01-01T00:00:00Z",
                                "tags": ["예시"],
                                "field": None,
                                "value": None,
                                "direction": None,
                                "resolves": None,
                                "retracts": None,
                            }
                        ],
                    }
                )
            raise AssertionError(task)

        def generate_text(self, task, system, user):
            raise AssertionError(task)

    config_file = tmp_path / "server.toml"
    config_file.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    with config_file.open("a") as stream:
        stream.write(f"\n[courier]\nmax_attempts = {failure_limit}\n")
    config = load_config(config_file, init_directories=True)
    llm = FakeLlm()
    runtime = build_runtime(config, plugins=Plugins(client=llm))
    cap = source().capture_id
    data = "가상 메모".encode()
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
    return runtime, config, llm


def test_capture_stop_recovery_and_committed_wiki_outputs(tmp_path):
    import hashlib
    import json
    import threading

    from weaver.runtime.assemble import Plugins, build_runtime
    from weaver.schema import ReviewPackage
    from weaver.wiki.store import WikiStore

    runtime, config, llm = runtime_capture(tmp_path)
    runtime._tick(threading.Event())
    package = ReviewPackage.model_validate_json(
        next(config.courier.proposals.glob("*.json")).read_bytes()
    )
    path = config.courier.proposals / "pipeline" / f"{package.source_id}.json"
    observed = PipelineItem.model_validate_json(path.read_bytes())
    assert observed.location == "user" and observed.status == "review"
    assert observed.history == ["spinner", "loom", "user"]
    assert set(observed.stage_times) == {"수집", "전송", "정리", "질문", "검토"}
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
    now = [0.0]
    runtime.scanner._clock = lambda: now[0]
    for attempt in range(5):
        now[0] += 10000
        runtime._tick(threading.Event())
        observed = PipelineItem.model_validate_json(path.read_bytes())
        assert observed.location == "loom"
        assert observed.status == ("apply_stopped" if attempt == 4 else "applying")
        assert observed.history == ["spinner", "loom", "user", "loom"]
    stopped_revision = observed.revision
    runtime.publisher.progress.publish()
    assert (
        PipelineItem.model_validate_json(path.read_bytes()).revision == stopped_revision
    )
    restarted = build_runtime(config, plugins=Plugins(client=llm))
    restarted._tick(threading.Event())
    still_stopped = PipelineItem.model_validate_json(path.read_bytes())
    assert still_stopped.status == "apply_stopped"
    assert still_stopped.revision == stopped_revision
    llm.failing = False
    restarted.scanner._clock = lambda: 1e12
    restarted._tick(threading.Event())
    observed = PipelineItem.model_validate_json(path.read_bytes())
    assert observed.location == "fabric" and observed.status == "done"
    assert observed.threads[0].kind == "기록"
    assert "반영" in observed.stage_times
    store = WikiStore(config.vault_root, config.state_root, settle_seconds=0)
    restarted.publisher.progress.wiki = store
    restarted.publisher.progress.vault = config.vault_root
    store.write("pages/예시.md", {}, f"가상 결과 [[summary {package.source_id}]]", None)
    store.write(
        "Digest/2026/01/2026-01-01.md",
        {},
        f"가상 Digest [[summary {package.source_id}]]",
        None,
    )
    restarted.publisher.progress.publish()
    observed = PipelineItem.model_validate_json(path.read_bytes())
    assert observed.location == "wiki" and observed.digest is not None
    assert "wiki갱신" in observed.stage_times
    page = observed.pages[0]
    assert (
        page.sha256
        == hashlib.sha256((config.vault_root / page.path).read_bytes()).hexdigest()
    )
    assert observed.digest.path.endswith("Digest/2026/01/2026-01-01.md")
    (store.root / "pages/예시.md").write_text("pending human edit")
    restarted.publisher.progress.publish()
    assert PipelineItem.model_validate_json(path.read_bytes()).pages == observed.pages


def test_output_path_contract_rejects_traversal_in_python_and_json_schema():
    import json

    from jsonschema import Draft202012Validator, FormatChecker
    from pydantic import ValidationError

    from weaver.schema import PipelinePage

    schema = PipelinePage.model_json_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for path in [
        "../private.md",
        "/absolute.md",
        "wiki/.hidden.md",
        "wiki//empty.md",
        "wiki\\page.md",
        "wiki/page.txt",
        "wiki/new\nline.md",
    ]:
        value = {"path": path, "updated_at": "2026-01-01T00:00:00Z", "sha256": "a" * 64}
        assert not validator.is_valid(value), json.dumps(value)
        try:
            PipelinePage.model_validate(value)
        except ValidationError:
            pass
        else:
            raise AssertionError(path)


def test_observation_write_failure_does_not_repeat_paid_summary(tmp_path, monkeypatch):
    import threading

    runtime, config, _ = runtime_capture(tmp_path)
    progress = runtime.publisher.progress
    original = progress.journal._write

    def broken(item):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(progress.journal, "_write", broken)
    runtime._tick(threading.Event())
    assert len(list(config.courier.proposals.glob("*.json"))) == 1
    assert runtime.ledger.counts()["done"] == 1
    monkeypatch.setattr(progress.journal, "_write", original)
    runtime._tick(threading.Event())
    item = PipelineItem.model_validate_json(
        next((config.courier.proposals / "pipeline").glob("*.json")).read_bytes()
    )
    assert item.status == "review"
    assert item.location == "user"


def test_custom_failure_limit_is_published_without_claiming_five(tmp_path):
    import json
    import threading

    from weaver.schema import ReviewPackage

    runtime, config, _ = runtime_capture(tmp_path, failure_limit=1)
    runtime._tick(threading.Event())
    package = ReviewPackage.model_validate_json(
        next(config.courier.proposals.glob("*.json")).read_bytes()
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
    path = config.courier.proposals / "pipeline" / f"{package.source_id}.json"
    observed = PipelineItem.model_validate_json(path.read_bytes())
    assert observed.status == "apply_stopped"
    assert observed.apply_failure_limit == 1
