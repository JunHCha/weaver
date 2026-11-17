"""Synthetic re-extraction migration; never opens user data or a real model."""

import hashlib
import json
from datetime import UTC, datetime

from pydantic import BaseModel
from server.tests.fake_responses import fixture_response
from typer.testing import CliRunner

from weaver.action import ActionEngine, ActionOutbox
from weaver.app import cli
from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.llm.ledger import Ledger, MemoryLedger
from weaver.loom.reextract import apply_report, render
from weaver.loom.reextract import reextract as plan_reextract
from weaver.loom.summary_review import Prepared
from weaver.loom.summary_state import SummaryState
from weaver.runtime import InstanceLock
from weaver.schema import ReviewPackage, ThreadDraft, Verdict

NOW = datetime(2026, 11, 1, tzinfo=UTC)
SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "src_01ARZ3NDEKTSV4RRFFQ69G5FAW"
SLOT = {"start": "2026-12-01T10:00:00+09:00", "end": None, "timezone": "Asia/Seoul"}


def draft(key, source=SOURCE, **changes):
    data = dict(
        thread_id=key,
        kind="기록",
        content="기존 사실",
        occurred_at=NOW,
        recorded_at=NOW,
        tags=["위버 작업", "TODO"],
        field=None,
        value=None,
        source_id=source,
        actor="ai_approved",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )
    data.update(changes)
    return ThreadDraft.model_validate(data)


class FakeLlm:
    def __init__(self, *, fail_body=None, schedule=False):
        self.calls = []
        self.fail_body = fail_body
        self.schedule = schedule

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        self.calls.append(task)
        if task == "loom.reextract_bootstrap":
            return fixture_response(
                schema,
                {
                    "entries": [
                        {"name": "Weaver", "kind": "topic", "aliases": ["위버 작업"]}
                    ]
                },
            )
        assert task == "loom.approve"
        data = json.loads(user)
        if data["body"] == self.fail_body:
            raise RuntimeError("synthetic failure")
        return fixture_response(
            schema,
            {
                "body": "새 승인 사실",
                "corrections": [],
                "new_entries": [],
                "threads": [
                    {
                        "kind": "일정" if self.schedule else "기록",
                        "content": "새 승인 사실",
                        "occurred_at": NOW.isoformat(),
                        "tags": ["위버 작업", "TODO"],
                        "field": "schedule" if self.schedule else None,
                        "value": SLOT if self.schedule else None,
                        "direction": None,
                        "resolves": None,
                        "retracts": None,
                    }
                ],
            },
        )

    def generate_text(self, task, system, user):
        raise AssertionError("unexpected text call")


def seed(root, *, source=SOURCE, action="approve", name="pkg", body="문서", old=None):
    state = SummaryState(root)
    package = ReviewPackage(
        schema_version=2,
        package_id=name,
        source_id=source,
        revision=1,
        draft_body=body,
        questions=[],
        stage="반영",
        stage_times={"반영": NOW},
    )
    verdict = Verdict(
        schema_version=2,
        package_id=name,
        revision=1,
        action=action,
        answers=[],
        edited_body=body if action == "edit" else None,
        feedback=None,
    )
    state.save_package(package, "비공개 원문은 철회에 들어가면 안 됨")
    if old is None:
        old = draft(
            name + "_old", source, actor="human" if action == "edit" else "ai_approved"
        )
    FabricStore(root / "fabric.sqlite3").append([old])
    with state.connect() as db:
        db.execute(
            "INSERT INTO reviews VALUES (?, 1, ?, ?, 'APPLIED')",
            (
                name,
                verdict.model_dump_json(),
                Prepared(threads=[old]).model_dump_json(),
            ),
        )
    return old


def hashes(root):
    return {
        str(p.relative_to(root)): (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mode,
        )
        for p in root.rglob("*")
        if p.is_file()
    }


def reextract(root, wiki, llm, *, apply=False, **kwargs):
    # Integration helper explicitly separates planning from model-free apply.
    report = plan_reextract(root, wiki, llm, **kwargs)
    return apply_report(root, report) if apply else report


def test_preview_is_readonly_and_shows_full_diff(tmp_path):
    seed(tmp_path)
    ContextDictionary(tmp_path)
    wiki = tmp_path / "wiki"
    (wiki / "pages").mkdir(parents=True)
    (wiki / "pages" / "Weaver.md").write_text(
        '---\nkind: topic\naliases: ["위버"]\n---\n# Weaver\n'
    )
    before = hashes(tmp_path)
    llm = FakeLlm()
    report = reextract(tmp_path, wiki, llm)
    assert hashes(tmp_path) == before
    assert llm.calls == ["loom.reextract_bootstrap", "loom.approve"]
    assert report.sources[0].old[0].tags == ["위버 작업", "TODO"]
    assert report.sources[0].new[0].tags == ["Weaver", "업무 운영"]
    output = render(report)
    assert "기존 Threads →" in output and "새 Threads" in output and "Weaver" in output
    assert "None=null" in output


def test_apply_is_idempotent_and_rejects_are_untouched(tmp_path):
    seed(tmp_path)
    rejected = seed(
        tmp_path,
        source=OTHER,
        name="reject",
        action="reject",
        old=draft("reject", OTHER, kind="거절", content=None, tags=[], actor="human"),
    )
    llm = FakeLlm()
    report = reextract(tmp_path, tmp_path / "wiki", llm, apply=True)
    assert all(s.done for s in report.sources)
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    first = fabric.entries(ThreadQuery())
    assert len(first) == 4
    assert [t for t in first if t.source_id == OTHER][0].thread_id == rejected.thread_id
    retract = next(t for t in first if t.retracts)
    assert retract.actor == "system" and "재추출" in retract.content
    assert "비공개" not in retract.content
    calls = llm.calls.copy()
    reextract(tmp_path, tmp_path / "wiki", llm, apply=True)
    assert llm.calls == calls
    assert fabric.entries(ThreadQuery()) == first
    assert ContextDictionary(tmp_path).resolve("위버 작업").name == "Weaver"
    assert not ContextDictionary(tmp_path).resolve("Weaver").human_edited


def test_human_tags_corrected_and_edit_extraction_replaced(tmp_path):
    seed(tmp_path, action="edit")
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append([draft("human", actor="human")])
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm(), apply=True)
    (plan,) = report.sources
    assert [t.thread_id for t in plan.old] == ["pkg_old"]
    assert plan.human_changes[0].old_tags == ["위버 작업", "TODO"]
    assert plan.human_changes[0].new_tags == ["Weaver"]
    threads = fabric.entries(ThreadQuery())
    assert next(t for t in threads if t.thread_id == "human").tags == ["Weaver"]
    assert not any(t.retracts == "human" for t in threads)
    assert any(t.kind == "태그정정" for t in threads)


def test_partial_llm_failure_keeps_each_source_consistent(tmp_path):
    seed(tmp_path, body="성공")
    seed(tmp_path, source=OTHER, name="other", body="실패")
    report = reextract(
        tmp_path, tmp_path / "wiki", FakeLlm(fail_body="실패"), apply=True
    )
    by_source = {s.source_id: s for s in report.sources}
    assert by_source[SOURCE].done and by_source[OTHER].error == "RuntimeError"
    threads = FabricStore(tmp_path / "fabric.sqlite3").entries(ThreadQuery())
    assert len([t for t in threads if t.source_id == SOURCE]) == 3
    assert len([t for t in threads if t.source_id == OTHER]) == 1
    retry = reextract(tmp_path, tmp_path / "wiki", FakeLlm(), apply=True)
    assert all(p.done for p in retry.sources)


def test_atomic_append_failure_replays_prepared_batch(tmp_path, monkeypatch):
    seed(tmp_path)
    original = FabricStore.append

    def fail(self, threads):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(FabricStore, "append", fail)
    llm = FakeLlm()
    report = reextract(tmp_path, tmp_path / "wiki", llm, apply=True)
    assert report.sources[0].error == "OSError"
    assert (
        len(
            FabricStore.open_readonly(tmp_path / "fabric.sqlite3").entries(
                ThreadQuery()
            )
        )
        == 1
    )
    assert ContextDictionary(tmp_path).resolve("Weaver")  # additions precede Fabric
    monkeypatch.setattr(FabricStore, "append", original)
    calls = llm.calls.copy()
    retry = reextract(tmp_path, tmp_path / "wiki", llm, apply=True)
    assert retry.sources[0].done and retry.sources[0].error is None
    assert llm.calls == calls


def test_schedule_lineage_avoids_remove_and_register_churn(tmp_path):
    old = draft("old_schedule", kind="일정", field="schedule", value=SLOT)
    seed(tmp_path, old=old)
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    outbox = ActionOutbox(tmp_path / "outbox.sqlite3")
    engine = ActionEngine(fabric, outbox)
    assert engine.run() == 1
    first = outbox.all()[0]
    outbox.finish(
        first.action_id,
        outcome="completed",
        external_id="event-7",
        error=None,
        attempts=1,
    )
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm(schedule=True), apply=True)
    assert report.sources[0].new[0].updates == old.thread_id
    engine.run()
    kinds = [a.kind for a in outbox.all()]
    assert kinds.count("calendar.register") == 1 and "calendar.remove" not in kinds
    assert all(a.lineage == first.lineage for a in outbox.all())
    assert engine.run() == 0


def test_source_filter_and_human_only_source(tmp_path):
    seed(tmp_path)
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append([draft("wiki_human", OTHER, actor="human")])
    before = fabric.entries(ThreadQuery())
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm(), apply=True, source=OTHER)
    assert len(report.sources) == 1 and not report.sources[0].new
    after = fabric.entries(ThreadQuery())
    assert [t for t in after if t.source_id == SOURCE] == [
        t for t in before if t.source_id == SOURCE
    ]
    assert next(t for t in after if t.thread_id == "wiki_human").tags == ["Weaver"]


def test_memory_ledger_preserves_existing_budget_without_writes(tmp_path):
    path = tmp_path / "llm.sqlite3"
    disk = Ledger(path)
    disk.reserve({"status": "pending"}, 1)
    before = hashes(tmp_path)
    memory = MemoryLedger(path)
    key = memory.reserve({"status": "pending"}, 2)
    memory.finish(key, {"status": "succeeded"}, 2)
    assert len(memory.records()) == 2
    assert len(disk.records()) == 1
    assert hashes(tmp_path) == before


def test_cli_noninteractive_apply_requires_yes_and_daemon_lock(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import weaver.app as app

    seed(tmp_path)
    settings = SimpleNamespace(
        state_root=tmp_path,
        vault_root=tmp_path / "wiki",
        llm=None,
        wiki=SimpleNamespace(vault_root=None, folder="Managed"),
        time=SimpleNamespace(timezone="Asia/Seoul"),
    )
    monkeypatch.setattr(app, "_load", lambda *args, **kwargs: settings)
    runner = CliRunner()
    before = hashes(tmp_path)
    result = runner.invoke(cli, ["reextract", "--apply"])
    assert (
        result.exit_code == 2 and "--yes" in result.output and "cp -a" in result.output
    )
    assert hashes(tmp_path) == before
    with InstanceLock(tmp_path / "weaver.lock"):
        result = runner.invoke(cli, ["reextract", "--apply", "--yes"])
    assert result.exit_code == 3 and "daemon holds" in result.output


def test_cli_json_preview_uses_fake_client(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import weaver.app as app
    import weaver.llm as llm

    seed(tmp_path)
    settings = SimpleNamespace(
        state_root=tmp_path,
        vault_root=tmp_path / "wiki",
        llm=None,
        wiki=SimpleNamespace(vault_root=None, folder="Managed"),
        time=SimpleNamespace(timezone="Asia/Seoul"),
    )
    monkeypatch.setattr(app, "_load", lambda *args, **kwargs: settings)
    monkeypatch.setattr(llm, "create_client", lambda *args, **kwargs: FakeLlm())
    before = hashes(tmp_path)
    result = CliRunner().invoke(cli, ["reextract", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert not data["applied"] and data["sources"][0]["new"][0]["tags"] == [
        "Weaver",
        "업무 운영",
    ]
    assert hashes(tmp_path) == before


def test_lost_append_reply_replays_without_duplicate_threads(tmp_path, monkeypatch):
    seed(tmp_path)
    append = FabricStore.append

    def lost_reply(self, threads):
        append(self, threads)
        raise OSError("synthetic lost response")

    monkeypatch.setattr(FabricStore, "append", lost_reply)
    llm = FakeLlm()
    report = reextract(tmp_path, tmp_path / "wiki", llm, apply=True)
    assert report.sources[0].error == "OSError"
    first = FabricStore.open_readonly(tmp_path / "fabric.sqlite3").entries(
        ThreadQuery()
    )
    monkeypatch.setattr(FabricStore, "append", append)
    calls = llm.calls.copy()
    retry = reextract(tmp_path, tmp_path / "wiki", llm, apply=True)
    assert retry.sources[0].done and retry.sources[0].error is None
    assert llm.calls == calls
    assert (
        FabricStore.open_readonly(tmp_path / "fabric.sqlite3").entries(ThreadQuery())
        == first
    )
    assert ContextDictionary(tmp_path).resolve("Weaver")


def test_old_edit_revisions_are_retracted_and_new_links_are_remapped(tmp_path):
    seed(tmp_path, action="edit")
    state = SummaryState(tmp_path)
    package, script = state.load_package("pkg", 1)
    second = package.model_copy(update={"revision": 2})
    state.save_package(second, script)
    verdict = Verdict(
        schema_version=2,
        package_id="pkg",
        revision=2,
        action="approve",
        answers=[],
        edited_body=None,
        feedback=None,
    )
    extra = draft("newer_old")
    FabricStore(tmp_path / "fabric.sqlite3").append([extra])
    with state.connect() as db:
        db.execute(
            "INSERT INTO reviews VALUES ('pkg', 2, ?, ?, 'APPLIED')",
            (verdict.model_dump_json(), Prepared(threads=[extra]).model_dump_json()),
        )
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm(), apply=True)
    assert {t.thread_id for t in report.sources[0].old} == {"pkg_old", "newer_old"}


def test_dictionary_bootstrap_preserves_wiki_title_and_short_alias(tmp_path):
    seed(tmp_path)
    from weaver.context import Page

    context = ContextDictionary(tmp_path)
    context.sync_from_pages([Page("프로젝트 긴 제목", "project")])
    context.set_kind("프로젝트 긴 제목", "project")
    wiki = tmp_path / "wiki"
    (wiki / "pages").mkdir(parents=True)
    (wiki / "pages" / "프로젝트 긴 제목.md").write_text(
        "---\nkind: project\n---\n본문\n"
    )

    class WikiLlm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            if task == "loom.reextract_bootstrap":
                self.calls.append(task)
                return fixture_response(
                    schema,
                    {
                        "entries": [
                            {
                                "name": "프로젝트 긴 제목",
                                "kind": "project",
                                "aliases": ["프로젝트"],
                            }
                        ]
                    },
                )
            return super().generate_json(task, system, user, schema)

    FabricStore(tmp_path / "fabric.sqlite3").append(
        [draft("wiki_name_use", actor="human", tags=["프로젝트"])]
    )
    llm = WikiLlm()
    report = reextract(tmp_path, wiki, llm, apply=True)
    assert llm.calls.count("loom.reextract_bootstrap") == 1
    assert report.bootstrap[0].name == "프로젝트 긴 제목"
    assert ContextDictionary(tmp_path).resolve("프로젝트").name == "프로젝트 긴 제목"


def test_json_apply_reports_partial_failure_with_nonzero_exit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import weaver.app as app
    import weaver.llm as llm

    seed(tmp_path)
    settings = SimpleNamespace(
        state_root=tmp_path,
        vault_root=tmp_path / "wiki",
        llm=None,
        wiki=SimpleNamespace(vault_root=None, folder="Managed"),
        time=SimpleNamespace(timezone="Asia/Seoul"),
    )
    monkeypatch.setattr(app, "_load", lambda *args, **kwargs: settings)
    monkeypatch.setattr(
        llm, "create_client", lambda *args, **kwargs: FakeLlm(fail_body="문서")
    )
    result = CliRunner().invoke(cli, ["reextract", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["sources"][0]["error"] == "RuntimeError"


def test_failed_source_does_not_leak_uncommitted_dictionary_to_next(
    tmp_path, monkeypatch
):
    seed(tmp_path)
    seed(tmp_path, source=OTHER, name="other")
    append = FabricStore.append

    def fail_first(self, threads):
        if threads and threads[0].source_id == SOURCE:
            raise OSError("synthetic first-source failure")
        return append(self, threads)

    class EntryLlm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            if task != "loom.approve":
                return super().generate_json(task, system, user, schema)
            # Approval only returns new_entries for names absent from its snapshot.
            names = {e["name"] for e in json.loads(user)["dictionary"]}
            self.calls.append(task)
            return fixture_response(
                schema,
                {
                    "body": "새 사실",
                    "corrections": [],
                    "new_entries": []
                    if "홍길동" in names
                    else [{"name": "홍길동", "kind": "person"}],
                    "threads": [
                        {
                            "kind": "기록",
                            "content": "새 사실",
                            "occurred_at": NOW.isoformat(),
                            "tags": ["홍길동"],
                            "field": None,
                            "value": None,
                            "direction": None,
                            "resolves": None,
                            "retracts": None,
                        }
                    ],
                },
            )

    monkeypatch.setattr(FabricStore, "append", fail_first)
    report = reextract(tmp_path, tmp_path / "wiki", EntryLlm(), apply=True)
    by_source = {p.source_id: p for p in report.sources}
    assert by_source[SOURCE].error == "OSError" and by_source[OTHER].done
    assert ContextDictionary(tmp_path).resolve("홍길동")
    assert any(e.name == "홍길동" for e in by_source[OTHER].additions)


def test_bootstrap_clean_rename_transcript_only_and_plan_tag_mapping(tmp_path):
    root = tmp_path / "state"
    seed(root)
    context = ContextDictionary(root)
    context.add_entry("지원팀 김철수 팀장님", "person", human_edited=False)
    context.add_entry("이름 미확인(수정 필요)", "person", human_edited=False)
    fabric = FabricStore(root / "fabric.sqlite3")
    fabric.append(
        [
            draft(
                "human",
                actor="human",
                tags=["지원팀 김철수 팀장님", "김 철수", "김철쑤"],
            )
        ]
    )

    class Llm(FakeLlm):
        def generate_json(self, task, system, user, schema):
            if task == "loom.reextract_bootstrap":
                for rule in (
                    "오타·오인식·띄어쓰기",
                    "직함·소속·메모",
                    "녹취 교정으로만",
                    "이름이 아니면",
                    "tag_corrections",
                ):
                    assert rule in system
                return fixture_response(
                    schema,
                    {
                        "entries": [
                            {
                                "name": "김철수",
                                "kind": "person",
                                "aliases": ["김 철수", "김철쑤"],
                            }
                        ],
                        "renames": [
                            {"observed": "지원팀 김철수 팀장님", "canonical": "김철수"},
                            {
                                "observed": "이름 미확인(수정 필요)",
                                "canonical": "확인 필요",
                            },
                        ],
                        "tag_corrections": [
                            {"observed": "김철쑤", "canonical": "김철수"}
                        ],
                    },
                )
            return fixture_response(
                schema,
                {"body": "김철수가 자료를 전달했다.", "corrections": [], "threads": []},
            )

    report = plan_reextract(root, tmp_path / "wiki", Llm())
    assert not report.errors and not report.sources[0].error
    plan = report.sources[0]
    assert [(c.observed, c.canonical) for c in plan.renames] == [
        ("지원팀 김철수 팀장님", "김철수")
    ]
    assert plan.human_changes[0].new_tags == ["김철수"]
    assert report.notices
    assert not any(e.aliases for e in plan.additions)
    applied = apply_report(root, report)
    assert applied.sources[0].done
    entry = context.resolve("김철수")
    assert not entry.human_edited and not entry.aliases
    assert "지원팀 김철수 팀장님" in entry.misrecognitions
    assert context.resolve("지원팀 김철수 팀장님") is None
    assert context.resolve("김철쑤") is None
    assert context.resolve("김 철수").name == "김철수"


def test_reextract_carries_area_and_counterpart_in_plan_preview_and_apply(tmp_path):
    seed(tmp_path)
    context = ContextDictionary(tmp_path)
    context.add_entry("김철수", "person")

    class PartyLlm(FakeLlm):
        def generate_json(self, task, system, user, schema):
            result = super().generate_json(task, system, user, schema)
            if task == "loom.approve":
                data = result.model_dump()
                data["threads"][0]["counterpart"] = "김철수"
                return fixture_response(schema, data)
            return result

    report = reextract(tmp_path, tmp_path / "wiki", PartyLlm())
    planned = report.sources[0].new[0]
    assert "업무 운영" in planned.tags
    assert planned.counterpart == "김철수"
    assert "counterpart=김철수" in render(report)
    apply_report(tmp_path, report)
    assert (
        next(
            t
            for t in FabricStore(tmp_path / "fabric.sqlite3").entries(ThreadQuery())
            if t.thread_id == planned.thread_id
        ).counterpart
        == "김철수"
    )


def test_area_contract_error_is_reported_per_source(tmp_path):
    seed(tmp_path, body="bad")
    seed(tmp_path, source=OTHER, name="good", body="good")

    class BadArea(FakeLlm):
        def generate_json(self, task, system, user, schema):
            result = super().generate_json(task, system, user, schema)
            if task == "loom.approve" and json.loads(user)["body"] == "bad":
                result.threads[0].tags = []
            return result

    report = reextract(tmp_path, tmp_path / "wiki", BadArea())
    plans = {plan.source_id: plan for plan in report.sources}
    assert plans[SOURCE].error == "SchemaMismatch"
    assert not plans[SOURCE].batch
    assert not plans[OTHER].error and plans[OTHER].new
