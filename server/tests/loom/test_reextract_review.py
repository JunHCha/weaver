"""Regressions found while reviewing the one-time real-data migration."""

import json
import sqlite3

import pytest

from weaver.action import ActionEngine, ActionOutbox
from weaver.context import ContextDictionary, Page
from weaver.fabric import FabricStore, ThreadQuery
from weaver.loom.reextract import reextract, render

from .test_reextract import OTHER, SLOT, SOURCE, FakeLlm, draft, hashes, seed


def test_only_managed_pages_reach_bootstrap(tmp_path):
    seed(tmp_path / "state")
    wiki = tmp_path / "vault" / "Managed"
    (wiki / "pages").mkdir(parents=True)
    (wiki / "pages" / "Weaver.md").write_text("---\nkind: project\n---\n")
    (tmp_path / "vault" / "unrelated-private-note.md").write_text("unrelated-secret")
    (wiki / "Digest").mkdir()
    (wiki / "Digest" / "private-digest.md").write_text("private-digest")
    (wiki / "query.md").write_text("query-secret")
    llm = CapturingLlm()
    report = reextract(tmp_path / "state", wiki, llm)
    prompt = llm.prompts[0]
    assert (
        "unrelated" not in prompt
        and "private-digest" not in prompt
        and "query-secret" not in prompt
    )
    assert "query" not in [p.name for p in report.bootstrap]


class CapturingLlm(FakeLlm):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.prompts = []

    def generate_json(self, task, system, user, schema):
        self.prompts.append(user)
        return super().generate_json(task, system, user, schema)


@pytest.mark.parametrize("changed", [False, True])
def test_baseline_schedule_replacement_never_registers(tmp_path, changed):
    from weaver.loom.reextract import apply_report

    seed(tmp_path, old=draft("legacy", kind="일정", field="schedule", value=SLOT))
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    outbox = ActionOutbox(tmp_path / "action-outbox.sqlite3")
    outbox.initialize(1)

    class ScheduleLlm(FakeLlm):
        def generate_json(self, task, system, user, schema):
            result = super().generate_json(task, system, user, schema)
            if changed and task == "loom.approve":
                data = result.model_dump()
                data["threads"][0]["value"]["start"] = "2026-12-02T10:00:00+09:00"
                return schema.model_validate(data)
            return result

    plan = reextract(tmp_path, tmp_path / "wiki", ScheduleLlm(schedule=True))
    if changed:
        assert "캘린더 수동 확인 필요" in render(plan)
    apply_report(tmp_path, plan)
    assert ActionEngine(fabric, outbox).run() == 0
    assert outbox.all() == []


def test_external_reference_skips_entire_source_preserving_open_requests(tmp_path):
    old = draft("request", kind="요청", direction="받음", tags=["Weaver"])
    seed(tmp_path, old=old)
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append(
        [draft("answer", OTHER, kind="결정", resolves=old.thread_id, tags=["Weaver"])]
    )
    before = fabric.state("Weaver").open_requests
    from weaver.loom.reextract import apply_report

    report = apply_report(tmp_path, reextract(tmp_path, tmp_path / "wiki", FakeLlm()))
    assert report.sources[0].skipped == "외부 참조로 건너뜀"
    assert report.sources[0].referencing_seqs == [2]
    assert fabric.state("Weaver").open_requests == before
    assert len(fabric.entries(ThreadQuery())) == 2


def test_automatic_alias_on_existing_wiki_entry_is_not_human_edit(tmp_path):
    context = ContextDictionary(tmp_path)
    context.sync_from_pages([Page("Weaver", "project")])
    entry = context.add_alias("Weaver", "위버 작업", human_edited=False)
    assert not entry.human_edited
    context.sync_from_pages([Page("Weaver", "organization", summary="최신")])
    assert context.resolve("위버 작업").kind == "organization"
    assert context.resolve("위버 작업").summary == "최신"


def test_junk_tag_resolving_to_generated_wiki_entry_is_dropped(tmp_path):
    seed(tmp_path)
    context = ContextDictionary(tmp_path)
    context.sync_from_pages([Page("TODO", "topic"), Page("확인", "topic")])
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    assert report.sources[0].new[0].tags == ["Weaver"]


def test_own_schedules_hidden_from_approval_and_later_updater_skips_source(tmp_path):
    seed(tmp_path, old=draft("old", kind="일정", field="schedule", value=SLOT))
    llm = CapturingLlm(schedule=True)
    reextract(tmp_path, tmp_path / "wiki", llm)
    assert json.loads(llm.prompts[-1])["existing_schedules"] == []
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append(
        [
            draft(
                "later", OTHER, kind="일정", field="schedule", value=SLOT, updates="old"
            )
        ]
    )
    plan = reextract(tmp_path, tmp_path / "wiki", FakeLlm(schedule=True))
    assert plan.sources[0].referencing_seqs == [2]


def test_saved_plan_apply_has_no_model_and_rejects_stale_fabric(tmp_path):
    from weaver.loom.reextract import apply_report, load_plan, save_plan

    seed(tmp_path / "state")
    report = reextract(tmp_path / "state", tmp_path / "wiki", FakeLlm())
    before = hashes(tmp_path / "state")
    path = tmp_path / "plan.json"
    save_plan(path, tmp_path / "state", report)
    assert hashes(tmp_path / "state") == before
    loaded = load_plan(path)
    fabric = FabricStore(tmp_path / "state" / "fabric.sqlite3")
    fabric.append([draft("intervening")])
    with pytest.raises(ValueError, match="다시 계획"):
        apply_report(tmp_path / "state", loaded)
    assert len(fabric.entries(ThreadQuery())) == 2


def test_dictionary_failure_precedes_fabric_append(tmp_path, monkeypatch):
    from weaver.loom.reextract import apply_report

    seed(tmp_path)
    plan = reextract(tmp_path, tmp_path / "wiki", FakeLlm())

    def fail(*args, **kwargs):
        raise OSError("synthetic dictionary failure")

    monkeypatch.setattr(ContextDictionary, "add_entry", fail)
    applied = apply_report(tmp_path, plan)
    assert applied.sources[0].error == "OSError"
    assert (
        len(
            FabricStore.open_readonly(tmp_path / "fabric.sqlite3").entries(
                ThreadQuery()
            )
        )
        == 1
    )


def test_corrupt_source_and_bad_page_are_reported_without_losing_other_sources(
    tmp_path,
):
    seed(tmp_path, name="a")
    seed(tmp_path, source=OTHER, name="b")
    with sqlite3.connect(tmp_path / "summary.sqlite3") as db:
        db.execute("UPDATE reviews SET verdict='bad json' WHERE id='a'")
    wiki = tmp_path / "wiki" / "pages"
    wiki.mkdir(parents=True)
    (wiki / "bad.md").write_text('---\nkind: "unclosed\n---\n')
    report = reextract(tmp_path, wiki.parent, FakeLlm())
    assert report.errors
    by_source = {p.source_id: p for p in report.sources}
    assert by_source[SOURCE].error
    assert by_source[OTHER].new


def test_missing_summary_is_reported_and_unused_bootstrap_is_omitted(tmp_path):
    FabricStore(tmp_path / "fabric.sqlite3")
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    assert report.errors and "summary.sqlite3" in report.errors[0]


def test_external_human_retraction_cannot_be_resurrected(tmp_path):
    seed(tmp_path)
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append([draft("human_retract", OTHER, actor="human", retracts="pkg_old")])
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    assert report.sources[0].skipped == "외부 참조로 건너뜀"
    assert not report.sources[0].batch


def test_bootstrap_caps_and_unrelated_notes_not_in_dictionary(tmp_path):
    from weaver.loom.reextract import (
        BOOTSTRAP_MAX_BYTES,
        BOOTSTRAP_MAX_ENTRIES,
        apply_report,
    )

    seed(tmp_path / "state")
    wiki = tmp_path / "vault" / "Managed"
    (wiki / "pages").mkdir(parents=True)
    for i in range(220):
        (wiki / "pages" / f"주제{i:03}.md").write_text(
            '---\nkind: topic\naliases: ["' + "별칭" * 45 + f'{i}"]\n---\n'
        )
    (tmp_path / "vault" / "비밀.md").write_text("무관한 개인 노트")
    llm = CapturingLlm()
    report = reextract(tmp_path / "state", wiki, llm)
    payload = json.loads(llm.prompts[0])
    assert len(llm.prompts[0].encode()) <= BOOTSTRAP_MAX_BYTES
    assert sum(len(v) for v in payload.values()) <= BOOTSTRAP_MAX_ENTRIES
    apply_report(tmp_path / "state", report)
    context = ContextDictionary(tmp_path / "state")
    assert not context.resolve("비밀")
    assert len(context.list_entries()) == 1  # only the tag actually used by facts


@pytest.mark.parametrize("change", ["dictionary", "summary", "outbox"])
def test_saved_plan_rejects_changed_inputs_before_any_write(tmp_path, change):
    from weaver.loom.reextract import apply_report

    seed(tmp_path)
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    if change == "dictionary":
        ContextDictionary(tmp_path).add_entry("사람이 추가", "person")
    elif change == "summary":
        with sqlite3.connect(tmp_path / "summary.sqlite3") as db:
            db.execute("UPDATE packages SET script='changed'")
    else:
        ActionOutbox(tmp_path / "action-outbox.sqlite3").initialize(1)
    before = hashes(tmp_path)
    with pytest.raises(ValueError, match="다시 계획"):
        apply_report(tmp_path, report)
    assert hashes(tmp_path) == before


def test_existing_entry_alias_preview_equals_apply(tmp_path):
    from weaver.loom.reextract import apply_report

    seed(tmp_path)
    context = ContextDictionary(tmp_path)
    context.sync_from_pages([Page("Weaver", "project")])
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    assert report.sources[0].additions[0].aliases == ["위버 작업"]
    apply_report(tmp_path, report)
    actual = ContextDictionary(tmp_path).resolve("위버 작업")
    assert actual.name == report.sources[0].new[0].tags[0] and not actual.human_edited


def test_plan_export_inside_state_is_refused_and_cli_apply_has_no_client(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import weaver.app as app
    import weaver.llm as llm
    from weaver.app import cli
    from weaver.loom.reextract import save_plan

    root = tmp_path / "state"
    seed(root)
    report = reextract(root, tmp_path / "wiki", FakeLlm())
    with pytest.raises(ValueError, match="outside state_root"):
        save_plan(root / "plan.json", root, report)
    path = tmp_path / "plan.json"
    save_plan(path, root, report)
    settings = SimpleNamespace(
        state_root=root,
        vault_root=tmp_path,
        wiki=SimpleNamespace(vault_root=None, folder="Managed"),
        llm=None,
        time=SimpleNamespace(timezone="Asia/Seoul"),
    )
    monkeypatch.setattr(app, "_load", lambda *args, **kwargs: settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("apply must never construct an LLM client")

    monkeypatch.setattr(llm, "create_client", forbidden)
    result = CliRunner().invoke(
        cli, ["reextract", "--apply", "--plan", str(path), "--yes", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["sources"][0]["done"]


def test_discard_stuck_plan_keeps_committed_facts(tmp_path, monkeypatch):
    from weaver.loom.reextract import apply_report, discard_plan

    seed(tmp_path)
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    append = FabricStore.append

    def fail(self, drafts):
        raise OSError("synthetic failure")

    monkeypatch.setattr(FabricStore, "append", fail)
    assert apply_report(tmp_path, report).sources[0].error
    assert discard_plan(tmp_path) == 1
    monkeypatch.setattr(FabricStore, "append", append)
    replanned = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    assert apply_report(tmp_path, replanned).sources[0].done


def test_cli_uses_overridden_wiki_root(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import weaver.app as app
    import weaver.llm as llm
    from weaver.app import cli

    seed(tmp_path / "state")
    managed = tmp_path / "overridden" / "Custom" / "pages"
    managed.mkdir(parents=True)
    (managed / "ManagedName.md").write_text("---\nkind: project\n---\n")
    (tmp_path / "unrelated.md").write_text("private")
    fake = CapturingLlm()
    settings = SimpleNamespace(
        state_root=tmp_path / "state",
        vault_root=tmp_path,
        wiki=SimpleNamespace(vault_root=tmp_path / "overridden", folder="Custom"),
        llm=None,
        time=SimpleNamespace(timezone="Asia/Seoul"),
    )
    monkeypatch.setattr(app, "_load", lambda *args, **kwargs: settings)
    monkeypatch.setattr(llm, "create_client", lambda *args, **kwargs: fake)
    result = CliRunner().invoke(cli, ["reextract", "--json"])
    assert result.exit_code == 0, result.output
    assert "ManagedName" in fake.prompts[0] and "unrelated" not in fake.prompts[0]


@pytest.mark.parametrize(
    "field", ["resolves", "blocked_by", "updates", "executes", "retracts", "correction"]
)
def test_every_external_reference_field_is_protected(tmp_path, field):
    kind = (
        "요청"
        if field in ("resolves", "blocked_by")
        else "일정"
        if field == "updates"
        else "기록"
    )
    old = draft(
        "protected",
        kind=kind,
        direction="받음" if kind == "요청" else None,
        field="schedule" if kind == "일정" else None,
        value=SLOT if kind == "일정" else None,
    )
    seed(tmp_path, old=old)
    changes = {field: old.thread_id}
    if field == "resolves":
        changes["kind"] = "결정"
    elif field in ("blocked_by", "updates"):
        changes.update(kind="일정", field="schedule", value=SLOT)
    elif field == "executes":
        changes.update(
            kind="실행",
            actor="system",
            tags=[],
            value={
                "action_id": "act_" + "0" * 32,
                "action": "calendar.register",
                "outcome": "completed",
                "external_id": "event",
                "error": None,
            },
        )
    elif field == "correction":
        changes.update(
            kind="태그정정",
            actor="human",
            correction={"target": old.thread_id, "remove": [], "add": ["수동"]},
        )
    FabricStore(tmp_path / "fabric.sqlite3").append(
        [draft("reference", OTHER, **changes)]
    )
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    plan = next(p for p in report.sources if p.source_id == SOURCE)
    assert plan.skipped == "외부 참조로 건너뜀" and plan.referencing_seqs == [2]


def test_uncheckpointed_wal_is_refused_without_creating_or_changing_files(tmp_path):
    seed(tmp_path)
    db = sqlite3.connect(tmp_path / "summary.sqlite3")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE packages SET script='pending writer'")
        db.commit()
        before = hashes(tmp_path)
        report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
        assert not report.valid and report.errors
        assert hashes(tmp_path) == before
    finally:
        db.close()


def test_corrupt_package_json_is_source_local(tmp_path):
    seed(tmp_path, name="a")
    seed(tmp_path, source=OTHER, name="b")
    with sqlite3.connect(tmp_path / "summary.sqlite3") as db:
        db.execute("UPDATE packages SET package='bad-json' WHERE id='a'")
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    assert any(p.error for p in report.sources)
    assert next(p for p in report.sources if p.source_id == OTHER).new


def test_corrupt_latest_package_never_falls_back_to_old_revision(tmp_path):
    from weaver.loom.summary_review import Prepared
    from weaver.loom.summary_state import SummaryState
    from weaver.schema import Verdict

    seed(tmp_path)
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
    with state.connect() as db:
        db.execute(
            "INSERT INTO reviews VALUES ('pkg', 2, ?, ?, 'APPLIED')",
            (
                verdict.model_dump_json(),
                Prepared(threads=[draft("latest")]).model_dump_json(),
            ),
        )
        db.execute("UPDATE packages SET package='bad-json' WHERE revision=2")
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm())
    plans = [p for p in report.sources if p.source_id == SOURCE]
    assert plans and all(p.error and not p.batch for p in plans)


def test_baseline_chain_preview_has_one_maintained_event(tmp_path):
    seed(tmp_path, old=draft("root", kind="일정", field="schedule", value=SLOT))
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append(
        [draft("latest", kind="일정", field="schedule", value=SLOT, updates="root")]
    )
    ActionOutbox(tmp_path / "action-outbox.sqlite3").initialize(1)
    report = reextract(tmp_path, tmp_path / "wiki", FakeLlm(schedule=True))
    assert len(report.sources[0].calendar_effects) == 1
    assert report.sources[0].calendar_effects[0].effect == "유지"
    assert report.sources[0].new[0].updates == "latest"
