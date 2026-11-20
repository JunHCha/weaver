"""Pipeline observations are durable, ordered and never expose uncommitted pages."""

import json
from datetime import UTC, datetime

import pytest
from server.tests.fake_responses import fixture_response

from weaver.runtime.progress import PipelineJournal, package_title
from weaver.schema import PipelineItem, PipelineTag, ReviewPackage, Source, ThreadDraft


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


def runtime_capture(tmp_path, failure_limit=5, counterpart=None):
    import hashlib
    import json

    from weaver.config import load_config
    from weaver.runtime.assemble import Plugins, build_runtime

    class FakeLlm:
        model = "fake"
        failing = True
        summary_body = "가상 요약"
        approved_body = None
        thread_content = "가상 기록"

        def generate_json(self, task, system, user, schema):
            if task == "loom.summary":
                return fixture_response(
                    schema,
                    {"script": "가상 메모", "body": self.summary_body, "questions": []},
                )
            if task == "loom.approve":
                if self.failing:
                    raise RuntimeError("synthetic error")
                return fixture_response(
                    schema,
                    {
                        "body": self.approved_body or self.summary_body,
                        "new_entries": [
                            {"name": "예시", "kind": "topic"},
                            {"name": "업무 운영", "kind": "area"},
                        ],
                        "corrections": [],
                        "threads": [
                            {
                                "kind": "기록",
                                "content": self.thread_content,
                                "occurred_at": "2026-01-01T00:00:00Z",
                                "tags": ["예시"],
                                "field": None,
                                "value": None,
                                "direction": None,
                                "resolves": None,
                                "retracts": None,
                            }
                        ],
                    },
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
        "hints": {"counterpart": counterpart},
    }
    (config.courier.outbox / manifest["payload"]).write_bytes(data)
    (config.courier.outbox / (cap + ".json")).write_text(json.dumps(manifest))
    return runtime, config, llm


def test_capture_stop_recovery_and_committed_wiki_outputs(tmp_path, caplog):
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
    runtime.scanner.ingest._clock = lambda: now[0]
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
    restarted.scanner.ingest._clock = lambda: 1e12
    restarted._tick(threading.Event())
    observed = PipelineItem.model_validate_json(path.read_bytes())
    assert observed.location == "fabric" and observed.status == "done"
    capture_wire = json.loads(path.read_text())
    assert "wiki_edit" not in capture_wire
    assert capture_wire["threads"][0]["content"] == "가상 기록"
    assert observed.threads[0].kind == "기록"
    assert observed.threads[0].content == "가상 기록"
    assert "반영" in observed.stage_times
    # An Action result is recorded in Fabric but never listed as a produced fact.
    from weaver.fabric import FabricStore

    fabric = FabricStore.from_config(config)
    fact = observed.threads[0].thread_id
    fabric.append(
        [
            ThreadDraft(
                thread_id="exe_" + "a" * 32,
                kind="실행",
                content="calendar.register completed",
                occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
                recorded_at=datetime(2026, 1, 2, tzinfo=UTC),
                tags=[],
                field=None,
                value={
                    "action_id": "act_" + "a" * 32,
                    "action": "calendar.register",
                    "outcome": "completed",
                    "external_id": "content://calendar/events/7",
                    "error": None,
                },
                source_id=package.source_id,
                actor="system",
                direction=None,
                resolves=None,
                retracts=None,
                correction=None,
                executes=fact,
            )
        ]
    )
    with caplog.at_level("WARNING"):
        restarted.publisher.progress.publish()
    assert "observation unavailable" not in caplog.text
    after = PipelineItem.model_validate_json(path.read_bytes())
    assert [t.kind for t in after.threads] == ["기록"]
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

    # A complete wiki rebuild removes old paths; preserve pending edits above,
    # but do not keep links to files that no longer exist.
    (store.root / "pages/예시.md").unlink()
    store.write("pages/새 업무.md", {}, f"재생성 [[summary {package.source_id}]]", None)
    restarted.publisher.progress.publish()
    rebuilt = PipelineItem.model_validate_json(path.read_bytes())
    assert {p.path for p in rebuilt.pages} == {"Weaver/pages/새 업무.md"}


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


@pytest.mark.parametrize("title", [None, "", "   ", "제품팀 주간 회의"])
@pytest.mark.parametrize("action", ["approve", "reject"])
def test_capture_card_legacy_title_and_thread_content_reach_phone_file(
    tmp_path, title, action
):
    """Use the capture→review→Fabric path, including a pre-title package on disk."""
    import threading

    from weaver.fabric import ThreadQuery
    from weaver.runtime.assemble import Plugins, build_runtime
    from weaver.schema import ReviewPackage

    runtime, config, llm = runtime_capture(tmp_path)
    llm.summary_body = "\n  제품팀은 다음 주에 검색 시안을 검토한다.\n담당자는 민수다."
    llm.thread_content = (
        "민수와 검색 시안을 다음 주 화요일 검토하기로 했다.\n회의에서 합의했다."
    )
    runtime._tick(threading.Event())
    package_path = next(config.courier.proposals.glob("*.json"))
    raw = json.loads(package_path.read_text())
    if title is None:
        raw.pop("title", None)  # Older persisted packages omit the optional title.
    else:
        raw["title"] = title
    progress = runtime.publisher.progress
    with progress.state.connect() as db:
        db.execute(
            "UPDATE packages SET package=? WHERE id=? AND revision=?",
            (json.dumps(raw), raw["package_id"], raw["revision"]),
        )
    package = ReviewPackage.model_validate(raw)
    expected_title = (
        title if title and title.strip() else "제품팀은 다음 주에 검색 시안을 검토한다."
    )
    # Exercise both immediate observation and restart reconciliation from the journal.
    progress.journal.update(package.source_id, title="수확한 데이터")
    progress.observe_package(package)
    assert progress.journal.get(package.source_id).title == expected_title
    progress.journal.update(package.source_id, title="수확한 데이터")
    progress.publish()
    path = config.courier.proposals / "pipeline" / f"{package.source_id}.json"
    assert PipelineItem.model_validate_json(path.read_bytes()).title == expected_title
    decision = config.courier.decisions / f"{package.package_id}.r1.json"
    decision.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "package_id": package.package_id,
                "revision": 1,
                "action": action,
                "answers": [],
                "edited_body": None,
                "feedback": None,
            }
        )
    )
    llm.failing = False
    runtime._tick(threading.Event())
    item = PipelineItem.model_validate_json(path.read_bytes())
    assert item.location == "fabric"
    assert item.status == ("rejected" if action == "reject" else "done")
    assert item.title == expected_title
    line = "제품팀은 다음 주에 검색 시안을 검토한다."
    # A summary that merely repeats the (derived) title is omitted.
    assert item.summary == ("" if action == "reject" or item.title == line else line)
    assert [(tag.name, tag.kind) for tag in item.related_tags] == (
        [] if action == "reject" else [("예시", "topic"), ("업무 운영", "area")]
    )
    stored = progress.fabric.entries(ThreadQuery())
    assert [(t.thread_id, t.kind, t.content) for t in item.threads] == [
        (t.thread_id, t.kind, t.content or "") for t in stored if t.kind != "실행"
    ]
    expected_content = "" if action == "reject" else llm.thread_content
    assert [t.content for t in item.threads] == [expected_content]
    assert json.loads(path.read_text())["threads"][0]["content"] == expected_content
    # APPLIED reconciliation must not change the title, content, or revision.
    journaled = progress.journal.get(package.source_id)
    progress._reconcile(progress.archive.get(package.source_id))
    assert progress.journal.get(package.source_id) == journaled
    assert PipelineItem.model_validate_json(path.read_bytes()) == item
    path.unlink()
    restarted = build_runtime(config, plugins=Plugins(client=llm))
    restarted.publisher.progress.publish()
    assert PipelineItem.model_validate_json(path.read_bytes()) == item


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# 제품팀 회의\n본문", "제품팀 회의"),
        ("  > - ** 검색 시안\n본문", "검색 시안"),
        ("\n---\ntitle: 비공개 메타\ntags: [검색]\n---\n## 회의 요약", "회의 요약"),
        ("---\ntitle: 메타\n---\n > - * # \n\n# 유효한 내용", "유효한 내용"),
        ("가" * 80, "가" * 80),
        ("가" * 81, "가" * 79 + "…"),
        ("  \n\t\n", "수확한 데이터"),
        ("# > - *\n\t", "수확한 데이터"),
        ("---\ntitle: 메타\n---\n", "수확한 데이터"),
        ("---\ntitle: 닫히지 않은 메타", "수확한 데이터"),
    ],
)
def test_legacy_package_title_skips_metadata_and_formats_readable_excerpt(
    body, expected
):
    package = ReviewPackage(
        schema_version=2,
        package_id="legacy-package",
        source_id=source().source_id,
        revision=1,
        draft_body=body if body.strip() else "기존 본문",
        questions=[],
        stage="검토",
        stage_times={},
    )
    if not body.strip():
        # The contract rejects blank drafts; the title helper also handles an
        # unvalidated in-memory legacy value defensively without relaxing it.
        with pytest.raises(ValueError):
            ReviewPackage.model_validate(package.model_dump() | {"draft_body": body})
        package = package.model_copy(update={"draft_body": body})
    assert package_title(package, "수확한 데이터") == expected


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


def test_unknown_hosts_are_omitted_from_projection(tmp_path):
    journal = PipelineJournal(tmp_path / "state", tmp_path / "proposals")
    item = journal.receive(source(), datetime.now(UTC))
    path = tmp_path / "proposals" / "pipeline" / f"{item.source_id}.json"
    assert "capture_hosts" not in json.loads(path.read_text())
    journal.update(item.source_id, capture_hosts=["unknown"])
    assert "capture_hosts" not in json.loads(path.read_text())
    journal.update(item.source_id, capture_hosts=["mac", "unknown"])
    assert json.loads(path.read_text())["capture_hosts"] == ["mac", "unknown"]


def test_pipeline_wire_keeps_counterpart_at_card_level_only():
    from pathlib import Path

    for name in ("PipelineItem.json", "PipelineItem-wiki-edit.json"):
        text = (
            Path(__file__).resolve().parents[2] / "schema" / "examples" / name
        ).read_text()
        item = PipelineItem.model_validate_json(text)
        wire = item.model_dump_json()
        assert "counterpart" in json.loads(wire)
        assert all(
            "counterpart" not in thread for thread in json.loads(wire)["threads"]
        )


def test_card_metadata_uses_approved_summary_explicit_contact_and_recovers_legacy(
    tmp_path,
):
    import threading

    from weaver.context import ContextDictionary
    from weaver.fabric import ThreadQuery
    from weaver.runtime.progress import card_tags

    runtime, config, llm = runtime_capture(tmp_path, counterpart="민수")
    context = ContextDictionary(config.state_root)
    context.add_entry("민수", "person")
    context.add_entry("검색", "area", aliases=("검색서비스",))
    llm.summary_body = "# 승인 전 초안"
    llm.approved_body = "---\ntitle: 메타\n---\n# 승인된 검색 검토\n둘째 줄"
    llm.thread_content = "검색 시안 검토를 합의했다."
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
    llm.failing = False
    runtime._tick(threading.Event())
    progress = runtime.publisher.progress
    item = progress.journal.get(package.source_id)
    assert item.summary == "둘째 줄"  # the heading line is skipped
    assert item.counterpart == "민수"
    assert [(t.name, t.kind) for t in item.related_tags] == [
        ("예시", "topic"),
        ("업무 운영", "area"),
    ]
    fact = progress.fabric.entries(ThreadQuery())[0]
    tags = card_tags(
        [
            fact.model_copy(update={"tags": ["검색서비스", "검색", "미분류", "민수"]}),
            fact.model_copy(update={"kind": "실행", "tags": ["실행전용"]}),
        ],
        context.snapshot(),
    )
    assert [(t.name, t.kind) for t in tags] == [
        ("검색", "area"),
        ("미분류", None),
        ("민수", "person"),
    ]
    # Pre-summary APPLIED records recover a summary from their committed Thread.
    with progress.state.connect() as db:
        prepared = json.loads(
            db.execute(
                "SELECT prepared FROM reviews WHERE id=?", (package.package_id,)
            ).fetchone()[0]
        )
        prepared.pop("summary", None)
        db.execute(
            "UPDATE reviews SET prepared=? WHERE id=?",
            (json.dumps(prepared), package.package_id),
        )
    progress.publish()
    recovered = progress.journal.get(package.source_id)
    assert recovered.summary == llm.thread_content
    path = config.courier.proposals / "pipeline" / f"{package.source_id}.json"
    wire = json.loads(path.read_text())
    assert wire["summary"] == recovered.summary and wire["counterpart"] == "민수"
    assert wire["related_tags"] == [
        {"name": "예시", "kind": "topic"},
        {"name": "업무 운영", "kind": "area"},
    ]
    path.unlink()
    progress.publish()
    assert (
        PipelineItem.model_validate_json(path.read_bytes()).summary == recovered.summary
    )
    assert progress.journal.get(package.source_id).revision == recovered.revision


def test_default_card_keys_are_omitted_and_present_when_set(tmp_path):
    journal = PipelineJournal(tmp_path / "state", tmp_path / "proposals")
    item = journal.receive(source(), datetime.now(UTC))
    path = tmp_path / "proposals" / "pipeline" / f"{item.source_id}.json"
    wire = json.loads(path.read_text())
    assert not {"summary", "counterpart", "related_tags"} & wire.keys()
    assert PipelineItem.model_validate(wire).related_tags == []
    journal.update(
        item.source_id,
        summary="요약",
        counterpart="민수",
        related_tags=[PipelineTag(name="검색", kind="area")],
    )
    wire = json.loads(path.read_text())
    assert wire["summary"] == "요약" and wire["counterpart"] == "민수"
    assert wire["related_tags"] == [{"name": "검색", "kind": "area"}]


def test_card_summary_bounds_skips_headings_and_falls_through():
    from weaver.runtime.progress import card_summary, card_summary_chain, card_tags

    long_line = "가" * 300
    summary = card_summary(f"# 제목\n{long_line}", "제목")
    assert len(summary) == 120 and summary.endswith("…")
    assert card_summary("# 제목만", "제목만") == ""
    assert card_summary("# 제목\n제목", "제목") == ""  # repeats the title
    assert card_summary_chain(["", "---\n---\n", "# 제목\n셋째"], "다른") == "셋째"
    assert card_summary_chain(["", "  \n"], "다른") == ""
    threads = [
        ThreadDraft.model_construct(
            kind="사실", content="x", tags=[f"태그{i}" for i in range(20)]
        )
    ]
    tags = card_tags(threads, None)
    assert [t.name for t in tags] == [f"태그{i}" for i in range(8)]


@pytest.mark.parametrize("publish_files", [False, True])
def test_projection_failure_does_not_interrupt_durable_observations(
    tmp_path, monkeypatch, publish_files
):
    journal = PipelineJournal(
        tmp_path / "state", tmp_path / "proposals", publish_files=publish_files
    )

    def fail(*args):
        raise OSError("synthetic shared-folder failure")

    monkeypatch.setattr("weaver.runtime.pipeline_projection.write_bytes_atomic", fail)
    first = journal.receive(source(), datetime.now(UTC))
    updated = journal.update(
        first.source_id, location="loom", summary="합성 요약", capture_hosts=["mac"]
    )
    journal.publish()
    restarted = PipelineJournal(
        tmp_path / "state", tmp_path / "proposals", publish_files=False
    )
    assert restarted.get(first.source_id) == updated
    assert updated.revision == first.revision + 1
    assert updated.history == ["spinner", "loom"]
    monkeypatch.undo()
    PipelineJournal(tmp_path / "state", tmp_path / "proposals").publish()
    data = (tmp_path / "proposals/pipeline" / f"{first.source_id}.json").read_bytes()
    assert PipelineItem.model_validate_json(data) == updated
