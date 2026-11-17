"""Synthetic end-to-end compilations: no models, network or user vaults."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.loom.tagging import tag_context, tag_snapshot
from weaver.schema import TagCorrection, ThreadDraft
from weaver.wiki.batch import BatchCompiler
from weaver.wiki.compiler import Summary
from weaver.wiki.promotion import ProjectLifecycle
from weaver.wiki.store import WikiStore

NOW = datetime(2026, 11, 17, tzinfo=UTC)
NAME = "라우터 교체"
AREA = "네트워크 운영"


def fact(key, *, day=0, **updates):
    return ThreadDraft(
        thread_id=key,
        kind="기록",
        content="새 라우터 전환을 검증하고 다음 주 구형 장비를 철거한다",
        occurred_at=NOW - timedelta(days=day),
        recorded_at=NOW - timedelta(days=day),
        tags=[AREA],
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        actor="ai_approved",
        field=None,
        value=None,
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    ).model_copy(update=updates)


class FakeWiki:
    def __init__(self):
        self.calls = 0
        self.short_task = False
        self.name = NAME

    def generate_json(self, task, system, user, schema):
        assert task == "wiki.compile_batch"
        self.calls += 1
        payload = json.loads(user)
        ids = [t["thread_id"] for t in payload["threads"]]
        return schema.model_validate(
            {
                "pages": [
                    {
                        "title": NAME,
                        "role": "project",
                        "summary": "구형 장비 철거까지 전환",
                        "thread_ids": ids,
                        "state_tag": AREA,
                        "aliases": [],
                        "body": f"# {NAME}\n\n전환과 검증 후 구형 장비를 철거한다.\n",
                    }
                ],
                "excluded_threads": [],
                "promotions": [
                    {
                        "name": self.name,
                        "area": AREA,
                        "goal": "라우터 전환",
                        "end_state": "검증 후 구형 장비 철거",
                        "short_task": self.short_task,
                        "thread_ids": ids,
                    }
                ],
            }
        )


def setup(tmp_path):
    context = ContextDictionary(tmp_path / "context")
    context.add_entry(AREA, "area")
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append([fact("one", day=5), fact("two")])
    store = WikiStore(
        tmp_path / "vault",
        tmp_path / "state",
        settle_seconds=0,
        clock=lambda: (NOW + timedelta(days=1)).timestamp(),
    )
    llm = FakeWiki()
    compiler = BatchCompiler(
        store,
        fabric,
        context,
        llm,
        tmp_path / "compile.sqlite3",
        lambda _: Summary("합성 자료", "전환·검증·철거 계획"),
    )
    return context, fabric, store, llm, compiler


def corrections(fabric):
    return [t for t in fabric.entries(ThreadQuery()) if t.kind == "태그정정"]


def test_compile_promotes_idempotently_and_preserves_original_rows(tmp_path):
    context, fabric, _, llm, compiler = setup(tmp_path)
    with sqlite3.connect(fabric.path) as db:
        before = db.execute("SELECT seq, payload FROM threads").fetchall()
    compiler.run()
    entry = context.resolve(NAME)
    assert entry.promoted and not entry.archived and not entry.human_edited
    assert len(corrections(fabric)) == 2
    assert all(
        t.actor == "system" and t.correction.add == [NAME] for t in corrections(fabric)
    )
    assert all(
        NAME in t.tags
        for t in fabric.entries(ThreadQuery())
        if t.thread_id in ("one", "two")
    )
    with sqlite3.connect(fabric.path) as db:
        assert (
            db.execute("SELECT seq, payload FROM threads WHERE seq<=2").fetchall()
            == before
        )
    assert tag_snapshot(context.snapshot()).resolve(NAME)
    compiler.run()
    assert llm.calls == 1 and len(corrections(fabric)) == 2
    print(
        "SYNTHETIC promotion: 2 system tag corrections; original rows unchanged; retry adds 0"
    )


@pytest.mark.parametrize("mode", ["completed", "30days"])
def test_compile_archives_on_completion_or_idle_tick(tmp_path, mode):
    context, fabric, store, llm, compiler = setup(tmp_path)
    compiler.run()
    if mode == "completed":
        fabric.append([fact("done", tags=[AREA, NAME], field="status", value="완료")])
    else:
        store.clock = lambda: (NOW + timedelta(days=30)).timestamp()
    compiler.run()
    assert context.resolve(NAME).archived
    assert not tag_snapshot(context.snapshot()).resolve(NAME)
    hints = tag_context(context.snapshot(), NAME, fabric.entries(ThreadQuery()))
    assert NAME not in hints["existing_tags"]
    assert all(e["name"] != NAME for e in hints["dictionary"])
    if mode == "30days":
        assert llm.calls == 1
    print(
        f"SYNTHETIC archive ({mode}): archived=True; excluded from extraction candidates"
    )


@pytest.mark.parametrize("mode", ["retract", "remove", "rename"])
def test_human_override_survives_recompile_restart_and_changed_candidate_name(
    tmp_path, mode
):
    context, fabric, store, llm, compiler = setup(tmp_path)
    compiler.run()
    first = corrections(fabric)[0]
    if mode == "rename":
        context.rename(NAME, "사람이 정한 전환 계획")
    elif mode == "retract":
        fabric.append([fact("undo", actor="human", retracts=first.thread_id)])
    else:
        fabric.append(
            [
                fact(
                    "remove",
                    actor="human",
                    kind="태그정정",
                    correction=TagCorrection(target="one", add=[], remove=[NAME]),
                )
            ]
        )
    fabric.append([fact("three")])
    if mode != "rename":
        llm.name = "라우터 전환 프로젝트"
    # Recreate the compiler to exercise durable suppression, then repeat.
    compiler = BatchCompiler(
        store, fabric, context, llm, compiler.state_path, compiler.summary
    )
    compiler.run()
    compiler.run()
    assert len([t for t in corrections(fabric) if t.actor == "system"]) == 2
    assert not context.resolve("라우터 전환 프로젝트")
    if mode == "rename":
        assert context.resolve(NAME).name == "사람이 정한 전환 계획"
    print(
        f"SYNTHETIC non-re-promotion ({mode}): system corrections remain 2 after restart/recompile"
    )


@pytest.mark.parametrize(
    "problem", ["short", "one_source_day", "mixed_area", "unknown_id", "collision"]
)
def test_ineligible_cluster_is_only_a_wiki_page(tmp_path, problem):
    context, fabric, _, llm, compiler = setup(tmp_path)
    if problem == "short":
        llm.short_task = True
    elif problem == "one_source_day":
        # A fresh log with two facts from one day/source is insufficient.
        compiler.fabric = FabricStore(tmp_path / "same-day.sqlite3")
        compiler.fabric.append([fact("one"), fact("two")])
    elif problem == "mixed_area":
        context.add_entry("시설 운영", "area")
        fabric.append([fact("other", tags=["시설 운영"])])
    elif problem == "unknown_id":
        lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
        from weaver.wiki.promotion import ProjectPromotion

        candidate = ProjectPromotion(
            name=NAME,
            area=AREA,
            goal="전환",
            end_state="철거",
            short_task=False,
            thread_ids=["one", "missing"],
        )
        assert not lifecycle.eligible(candidate, fabric.entries(ThreadQuery()))
        return
    else:
        context.add_entry(NAME, "topic")
    compiler.run()
    assert not corrections(compiler.fabric)
    assert not context.resolve(NAME).promoted


def test_multi_source_same_day_can_promote_a_substantial_goal(tmp_path):
    context, _, _, _, compiler = setup(tmp_path)
    compiler.fabric = FabricStore(tmp_path / "multi-source.sqlite3")
    compiler.fabric.append(
        [fact("one"), fact("two", source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAW")]
    )
    compiler.run()
    assert context.resolve(NAME).promoted


def test_new_project_evidence_gets_one_correction(tmp_path):
    _, fabric, _, _, compiler = setup(tmp_path)
    compiler.run()
    fabric.append([fact("three")])
    compiler.run()
    assert len(corrections(fabric)) == 3
    assert (
        NAME
        in next(t for t in fabric.entries(ThreadQuery()) if t.thread_id == "three").tags
    )


def test_append_failure_replays_prepared_correction_timestamp(tmp_path, monkeypatch):
    _, fabric, _, _, compiler = setup(tmp_path)
    original = fabric.append

    def fail(drafts):
        raise OSError("interrupted")

    monkeypatch.setattr(fabric, "append", fail)
    with pytest.raises(OSError, match="interrupted"):
        compiler.run()
    with sqlite3.connect(compiler.state_path) as db:
        prepared = [
            ThreadDraft.model_validate_json(row[0])
            for row in db.execute("SELECT payload FROM wiki_project_corrections")
        ]
    monkeypatch.setattr(fabric, "append", original)
    compiler.store.clock = lambda: (NOW + timedelta(days=2)).timestamp()
    compiler.run()
    assert {t.thread_id: t.recorded_at for t in corrections(fabric)} == {
        t.thread_id: t.recorded_at for t in prepared
    }
