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
    _, fabric, _, llm, compiler = setup(tmp_path)
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
    assert llm.calls == 1
    assert {t.thread_id: t.recorded_at for t in corrections(fabric)} == {
        t.thread_id: t.recorded_at for t in prepared
    }


def proposal(name=NAME, ids=None, **updates):
    return (
        dict(
            name=name,
            area=AREA,
            goal="라우터 전환",
            end_state="구형 장비 철거",
            short_task=False,
            thread_ids=ids or ["one", "two"],
        )
        | updates
    )


def test_invalid_promotion_items_do_not_discard_pages_or_digests(tmp_path, caplog):
    from zoneinfo import ZoneInfo

    from weaver.runtime.wiki import WikiJob

    context, fabric, store, llm, _ = setup(tmp_path)
    original = llm.generate_json

    def response(task, system, user, schema):
        data = original(task, system, user, schema).model_dump()
        data["promotions"] = [
            proposal(name="하루 처리", ids=["one"]) | {"short_task": True},
            proposal(name="비밀" * 51),
            proposal(name="목표 초과", goal="숨김" * 251),
            proposal(name="중복 근거", ids=["one", "one"]),
            proposal(),
            proposal(),
            None,
        ]
        data["digests"] = [
            {
                "path": t["path"],
                "sentences": [{"text": "합성 요약", "evidence": t["evidence"]}],
            }
            for t in json.loads(user)["digest_targets"]
        ]
        return schema.model_validate(data)

    llm.generate_json = response
    job = WikiJob(
        store,
        fabric,
        context,
        llm,
        tmp_path / "job",
        ZoneInfo("Asia/Seoul"),
        lambda _: Summary("합성 자료", "합성 본문"),
    )
    job.run()
    assert job.compiler.timezone.key == "Asia/Seoul"
    assert store.read(f"pages/{NAME}.md")
    assert list((store.root / "Digest").rglob("*.md"))
    assert len(corrections(fabric)) == 2 and context.resolve(NAME).promoted
    assert "invalid_item" in caplog.text and "duplicate_name" in caplog.text
    assert "비밀" not in caplog.text and "숨김" not in caplog.text


@pytest.mark.parametrize(
    "times, expected",
    [
        (("2026-11-16T15:10:00+00:00", "2026-11-17T14:50:00+00:00"), False),
        (("2026-11-17T14:50:00+00:00", "2026-11-17T15:10:00+00:00"), True),
    ],
)
def test_project_days_use_configured_seoul_dates_without_extra_span_rule(
    tmp_path, times, expected
):
    from zoneinfo import ZoneInfo

    context, _, _, _, compiler = setup(tmp_path)
    fabric = FabricStore(tmp_path / "boundary.db")
    fabric.append(
        [
            fact(
                key,
                occurred_at=datetime.fromisoformat(at),
                recorded_at=datetime.fromisoformat(at),
            )
            for key, at in zip(("one", "two"), times, strict=True)
        ]
    )
    lifecycle = ProjectLifecycle(
        compiler.state_path, context, fabric, ZoneInfo("Asia/Seoul")
    )
    lifecycle.apply(
        [proposal()], fabric.entries(ThreadQuery()), NOW + timedelta(days=2)
    )
    assert bool(corrections(fabric)) == expected


@pytest.mark.parametrize(
    "age, expected",
    [
        (timedelta(days=29, hours=23), True),
        (timedelta(days=30), False),
        (timedelta(days=31), False),
    ],
)
def test_promotion_activity_uses_source_time_even_right_after_reextract(
    tmp_path, age, expected
):
    context, _, _, _, compiler = setup(tmp_path)
    fabric = FabricStore(tmp_path / "source-time.db")
    fabric.append(
        [
            fact("one", occurred_at=NOW - age - timedelta(days=1), recorded_at=NOW),
            fact("two", occurred_at=NOW - age, recorded_at=NOW),
        ]
    )
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.apply([proposal()], fabric.entries(ThreadQuery()), NOW)
    assert bool(corrections(fabric)) == expected


def test_future_source_time_is_capped_at_recording_time(tmp_path):
    context, _, _, _, compiler = setup(tmp_path)
    fabric = FabricStore(tmp_path / "future.db")
    fabric.append(
        [
            fact(
                "one",
                occurred_at=NOW + timedelta(days=1),
                recorded_at=NOW - timedelta(days=31),
            ),
            fact(
                "two",
                occurred_at=NOW + timedelta(days=2),
                recorded_at=NOW - timedelta(days=30),
            ),
        ]
    )
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.apply([proposal()], fabric.entries(ThreadQuery()), NOW)
    assert not corrections(fabric)


@pytest.mark.parametrize(
    "age, expected", [(timedelta(days=29, hours=23), False), (timedelta(days=30), True)]
)
def test_promoted_archive_boundary_ignores_reextraction_recording_time(
    tmp_path, age, expected
):
    context, fabric, _, _, compiler = setup(tmp_path)
    compiler.run()
    fabric.append(
        [fact("reextracted", tags=[AREA, NAME], occurred_at=NOW, recorded_at=NOW + age)]
    )
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.refresh(NOW + age)
    assert context.resolve(NAME).archived == expected


def test_human_projects_only_archive_on_completion_and_new_record_unarchives(tmp_path):
    context, _, _, _, compiler = setup(tmp_path)
    context.add_entry("직접 만든 프로젝트", "project")
    fabric = FabricStore(tmp_path / "manual.db")
    fabric.append([fact("old", tags=[AREA, "직접 만든 프로젝트"], day=90)])
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.refresh(NOW)
    assert not context.resolve("직접 만든 프로젝트").archived
    fabric.append(
        [fact("done", tags=[AREA, "직접 만든 프로젝트"], field="status", value="done")]
    )
    lifecycle.refresh(NOW)
    assert context.resolve("직접 만든 프로젝트").archived
    fabric.append([fact("reopened", tags=[AREA, "직접 만든 프로젝트"])])
    lifecycle.refresh(NOW)
    lifecycle.refresh(NOW)
    assert not context.resolve("직접 만든 프로젝트").archived
    assert not context.resolve("직접 만든 프로젝트").promoted


def test_archive_does_not_repromote_and_new_tagged_record_reopens(tmp_path):
    context, fabric, store, _, compiler = setup(tmp_path)
    compiler.run()
    later = NOW + timedelta(days=30)
    store.clock = lambda: later.timestamp()
    compiler.run()
    assert context.resolve(NAME).archived
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.apply([proposal()], fabric.entries(ThreadQuery()), later)
    assert len(corrections(fabric)) == 2 and context.resolve(NAME).archived
    fabric.append(
        [fact("fresh", tags=[AREA, NAME], occurred_at=later, recorded_at=later)]
    )
    compiler.run()
    assert not context.resolve(NAME).archived
    assert context.resolve(NAME).promoted
    assert len(corrections(fabric)) == 2


def test_alias_and_kind_edits_do_not_revert_promotion(tmp_path):
    context, fabric, _, _, compiler = setup(tmp_path)
    compiler.run()
    context.add_alias(NAME, "라우터 이전")
    fabric.append([fact("three")])
    compiler.run()
    assert len(corrections(fabric)) == 3
    context.set_kind(NAME, "topic")
    compiler.run()
    context.set_kind(NAME, "project")
    fabric.append([fact("four")])
    compiler.run()
    assert len(corrections(fabric)) == 4
    assert not context.resolve(NAME).promotion_reverted


def test_alias_removal_is_resolved_and_retracted_promotion_is_not_a_candidate(tmp_path):
    context, fabric, _, _, compiler = setup(tmp_path)
    compiler.run()
    context.add_alias(NAME, "전환 별명")
    fabric.append(
        [
            fact(
                "undo",
                actor="human",
                kind="태그정정",
                correction=TagCorrection(target="one", add=[], remove=["전환 별명"]),
            )
        ]
    )
    fabric.append([fact("three")])
    compiler.run()
    assert not context.resolve(NAME).promoted
    assert context.resolve(NAME).promotion_reverted
    assert tag_snapshot(context.snapshot()).resolve(NAME) is None
    hints = tag_context(context.snapshot(), NAME, fabric.entries(ThreadQuery()))
    assert NAME not in hints["existing_tags"]
    assert len([t for t in corrections(fabric) if t.actor == "system"]) == 2


def test_revert_blocks_disjoint_thread_ids_from_the_same_area_and_sources(tmp_path):
    context, fabric, _, _, compiler = setup(tmp_path)
    compiler.run()
    fabric.append(
        [fact("undo", actor="human", retracts=corrections(fabric)[0].thread_id)]
    )
    fabric.append([fact("new-one", day=1), fact("new-two")])
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.apply(
        [proposal(name="새로 부른 전환", ids=["new-one", "new-two"])],
        fabric.entries(ThreadQuery()),
        NOW + timedelta(days=1),
    )
    assert context.resolve("새로 부른 전환") is None
    assert not context.resolve(NAME).promoted
    assert tag_snapshot(context.snapshot()).resolve(NAME) is None


def test_revert_does_not_suppress_another_existing_project_sharing_evidence(tmp_path):
    context, fabric, _, _, compiler = setup(tmp_path)
    compiler.run()
    context.add_entry("별도 프로젝트", "project")
    fabric.append(
        [
            fact(
                "share",
                actor="human",
                kind="태그정정",
                correction=TagCorrection(
                    target="one", add=["별도 프로젝트"], remove=[]
                ),
            ),
            fact("undo", actor="human", retracts=corrections(fabric)[0].thread_id),
        ]
    )
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.apply(
        [proposal(name="별도 프로젝트")],
        fabric.entries(ThreadQuery()),
        NOW + timedelta(days=1),
    )
    two = next(t for t in fabric.entries(ThreadQuery()) if t.thread_id == "two")
    assert "별도 프로젝트" in two.tags
    assert context.resolve(NAME).promotion_reverted
    assert not context.resolve("별도 프로젝트").promotion_reverted


def test_evidence_with_an_active_project_attaches_to_it_instead_of_competing(tmp_path):
    context, fabric, _, _, compiler = setup(tmp_path)
    compiler.run()
    fabric.append([fact("three")])
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.apply(
        [proposal(name="경쟁 이름", ids=["two", "three"])],
        fabric.entries(ThreadQuery()),
        NOW + timedelta(days=1),
    )
    assert context.resolve("경쟁 이름") is None
    assert (
        NAME
        in next(t for t in fabric.entries(ThreadQuery()) if t.thread_id == "three").tags
    )


@pytest.mark.parametrize("areas", [[], [AREA, "시설 운영"]])
def test_evidence_requires_exactly_one_area_tag_per_thread(tmp_path, areas):
    context, _, _, _, compiler = setup(tmp_path)
    context.add_entry("시설 운영", "area")
    fabric = FabricStore(tmp_path / "area-count.db")
    fabric.append([fact("one", day=1, tags=areas), fact("two")])
    lifecycle = ProjectLifecycle(compiler.state_path, context, fabric)
    lifecycle.apply(
        [proposal()], fabric.entries(ThreadQuery()), NOW + timedelta(days=1)
    )
    assert not corrections(fabric)


def test_partial_append_retry_does_not_call_llm_again(tmp_path, monkeypatch):
    context, fabric, _, llm, compiler = setup(tmp_path)
    original = fabric.append

    def partial(drafts):
        if len(corrections(fabric)) == 1:
            raise OSError("after first correction")
        return original(drafts)

    monkeypatch.setattr(fabric, "append", partial)
    with pytest.raises(OSError, match="after first correction"):
        compiler.run()
    assert compiler.bookmark() == 2
    assert len(corrections(fabric)) == 1
    monkeypatch.setattr(fabric, "append", original)
    compiler.run()
    assert llm.calls == 1 and len(corrections(fabric)) == 2
    assert context.resolve(NAME).promoted


def test_prepared_batch_cache_survives_crash_before_bookmark(tmp_path, monkeypatch):
    _, fabric, _, llm, compiler = setup(tmp_path)
    original = ProjectLifecycle.apply

    def interrupted(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("prepared before bookmark")

    monkeypatch.setattr(ProjectLifecycle, "apply", interrupted)
    with pytest.raises(OSError, match="before bookmark"):
        compiler.run()
    assert compiler.bookmark() == 0
    assert not corrections(fabric)
    monkeypatch.setattr(ProjectLifecycle, "apply", original)
    compiler.run()
    assert llm.calls == 1
    assert compiler.bookmark() == 2 and len(corrections(fabric)) == 2


def test_expired_pending_append_does_not_create_a_stale_active_project(
    tmp_path, monkeypatch
):
    context, fabric, store, llm, compiler = setup(tmp_path)
    original = fabric.append

    def fail(drafts):
        raise OSError("interrupted")

    monkeypatch.setattr(fabric, "append", fail)
    with pytest.raises(OSError):
        compiler.run()
    monkeypatch.setattr(fabric, "append", original)
    store.clock = lambda: (NOW + timedelta(days=30)).timestamp()
    compiler.run()
    assert not corrections(fabric) and llm.calls == 1
    assert context.resolve(NAME).archived
    assert tag_snapshot(context.snapshot()).resolve(NAME) is None
