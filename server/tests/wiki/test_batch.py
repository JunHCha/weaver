"""Whole-corpus generation with fake models and isolated temporary vaults."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from server.tests.fake_responses import edit_response

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.schema import ThreadDraft
from weaver.wiki.batch import BatchCompiler
from weaver.wiki.compiler import Summary
from weaver.wiki.store import HumanEditPending, WikiStore


def draft(key, tags):
    return ThreadDraft(
        thread_id=key,
        kind="기록",
        content=f"사실 {key}",
        occurred_at=datetime(2026, 11, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 11, 1, tzinfo=UTC),
        tags=tags,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        actor="ai_approved",
        field=None,
        value=None,
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )


class Llm:
    def __init__(self):
        self.calls = []
        self.transform = lambda data: data

    def generate_json(self, task, system, user, schema):
        self.calls.append((task, json.loads(user)))
        payload = json.loads(user)
        if task == "wiki.human_edit":
            return edit_response(schema, user, {"changes": []})
        pages = []
        for title, ids in [("네트워크 운영", ["one"]), ("업무 도구", ["two"])]:
            if not set(ids).issubset({t["thread_id"] for t in payload["threads"]}):
                continue
            previous = next(
                (
                    p["body"]
                    for p in payload["existing_pages"]
                    if p["path"] == f"pages/{title}.md"
                ),
                "",
            )
            pages.append(
                {
                    "title": title,
                    "role": "operation",
                    "summary": title + " 현황",
                    "thread_ids": ids,
                    "state_tag": None,
                    "aliases": [],
                    "body": f"# {title}\n\n서술형 본문. [[pages/{'업무 도구' if title == '네트워크 운영' else '네트워크 운영'}]]\n"
                    + ("사람 표현 유지\n" if "사람 표현 유지" in previous else ""),
                }
            )
        return schema.model_validate(
            self.transform(
                {
                    "pages": pages,
                    "excluded_threads": [],
                    "digests": [
                        {
                            "path": target["path"],
                            "sentences": [
                                {"text": "요약", "evidence": target["evidence"]}
                            ],
                        }
                        for target in payload["digest_targets"]
                    ],
                }
            )
        )

    def generate_text(self, *args):
        raise AssertionError("batch generation must use a single structured call")


def setup(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    fabric.append([draft("one", ["네트워크", "TODO"]), draft("two", ["도구", "확인"])])
    llm = Llm()
    compiler = BatchCompiler(
        store,
        fabric,
        ContextDictionary(tmp_path / "context"),
        llm,
        tmp_path / "compiler.db",
        lambda _: Summary("승인된 통화", "요약 전문과 제약 조건"),
    )
    return store, fabric, llm, compiler


def test_whole_snapshot_once_summaries_and_index_without_generic_pages(tmp_path):
    store, fabric, llm, compiler = setup(tmp_path)
    original = fabric.entries(ThreadQuery())
    compiler.run()
    assert len(llm.calls) == 1 and llm.calls[0][0] == "wiki.compile_batch"
    payload = llm.calls[0][1]
    assert len(payload["threads"]) == 2
    assert payload["sources"][0]["body"] == "요약 전문과 제약 조건"
    assert "current_states" in payload
    assert not (store.root / "pages/TODO.md").exists()
    assert not (store.root / "pages/확인.md").exists()
    assert "사실 one" not in store.read("pages/네트워크 운영.md")[1]
    compiler.refresh_index()
    assert "[[pages/네트워크 운영]]" in store.read("index.md")[1]
    assert "[[schema|Wiki 운영 규칙]]" in store.read("index.md")[1]
    assert compiler.bookmark() == 2
    compiler.run()
    assert len(llm.calls) == 1
    assert fabric.entries(ThreadQuery()) == original
    assert store.scan(store.clock() + 100) == []


@pytest.mark.parametrize(
    "problem", ["link", "anchor", "evidence", "missing", "title", "generic"]
)
def test_invalid_batch_never_publishes_pages_or_advances_bookmark(tmp_path, problem):
    store, _, llm, compiler = setup(tmp_path)

    def change(data):
        if problem == "link":
            data["pages"][0]["body"] += "[[pages/유령]]"
        if problem == "anchor":
            data["pages"][0]["body"] += "[[pages/업무 도구#없는 제목]]"
        if problem == "evidence":
            data["pages"][0]["thread_ids"] = ["unknown"]
        if problem == "missing":
            data["pages"] = data["pages"][:1]
        if problem == "title":
            data["pages"][0]["title"] = "bad#heading"
        if problem == "generic":
            data["pages"][0]["title"] = "TODO"
        return data

    llm.transform = change
    with pytest.raises(ValueError):
        compiler.run()
    assert not list((store.root / "pages").glob("*.md"))
    assert compiler.bookmark() == 0


def test_partial_write_resumes_same_batch_without_another_call(tmp_path, monkeypatch):
    store, _, llm, compiler = setup(tmp_path)
    original_write = store.write

    def write(path, *args, **kwargs):
        if path == "pages/업무 도구.md":
            raise HumanEditPending(path)
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(store, "write", write)
    for _ in range(3):
        with pytest.raises(HumanEditPending):
            compiler.run()
        assert compiler.bookmark() == 0
    assert len(llm.calls) == 1
    monkeypatch.setattr(store, "write", original_write)
    compiler.run()
    assert len(llm.calls) == 1 and compiler.bookmark() == 2


def test_unsettled_human_edit_blocks_before_generation(tmp_path):
    store, fabric, llm, compiler = setup(tmp_path)
    compiler.run()
    page = store.root / "pages/네트워크 운영.md"
    page.write_text(page.read_text() + "사람 표현 유지\n")
    fabric.append([draft("three", ["네트워크"])])
    with pytest.raises(HumanEditPending):
        compiler.run()
    assert len(llm.calls) == 1
    assert "사람 표현 유지" in page.read_text()


def test_human_typo_after_partial_write_invalidates_cached_batch(tmp_path, monkeypatch):
    store, _, llm, compiler = setup(tmp_path)
    original_write = store.write

    def write(path, *args, **kwargs):
        if path == "pages/업무 도구.md":
            raise HumanEditPending(path)
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(store, "write", write)
    with pytest.raises(HumanEditPending):
        compiler.run()
    page = store.root / "pages/네트워크 운영.md"
    props, body, base = store.read("pages/네트워크 운영.md")
    # A settled formatting-only edit may change the base without appending a Thread.
    store.write(
        "pages/네트워크 운영.md",
        props,
        body.replace("서술형 본문.", "서술형 본문.\n사람 표현 유지"),
        base,
    )
    monkeypatch.setattr(store, "write", original_write)
    compiler.run()
    assert len(llm.calls) == 2
    assert "사람 표현 유지" in page.read_text()
    with sqlite3.connect(compiler.state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_invalid_batch_is_suppressed_until_snapshot_changes(tmp_path, caplog):
    _, fabric, llm, compiler = setup(tmp_path)
    llm.transform = lambda data: (
        data | {"excluded_threads": [{"thread_id": "secret", "reason": "private"}]}
    )
    with pytest.raises(ValueError):
        compiler.run()
    compiler.run()
    assert len(llm.calls) == 1
    assert "private" not in caplog.text
    fabric.append([draft("three", ["new"])])
    with pytest.raises(ValueError):
        compiler.run()
    assert len(llm.calls) == 2


def test_validation_reason_is_specific_and_redacted(tmp_path, caplog):
    _, _, llm, compiler = setup(tmp_path)

    def invalid(data):
        data["pages"][0]["body"] += "[[private secret]]"
        return data

    llm.transform = invalid
    with pytest.raises(ValueError):
        compiler.run()
    with sqlite3.connect(compiler.state_path) as db:
        reason = db.execute("SELECT reason FROM batch_failures").fetchone()[0]
    assert "wiki batch contains an unknown link" in reason
    assert "private secret" not in reason + caplog.text


def test_job_compiles_and_updates_digest_in_one_call(tmp_path):
    from zoneinfo import ZoneInfo

    from weaver.runtime.wiki import WikiJob

    store, fabric, llm, _ = setup(tmp_path)
    # Use a fresh Fabric with exactly one Thread for the call-count regression.
    fabric = FabricStore(tmp_path / "one-thread.db")
    fabric.append([draft("one", ["네트워크"])])

    def one_page(result):
        page = result["pages"][0]
        page["body"] = "# 네트워크 운영\n\n서술형 본문.\n"
        return result | {"pages": [page]}

    llm.transform = one_page
    job = WikiJob(
        store,
        fabric,
        ContextDictionary(tmp_path / "job-context"),
        llm,
        tmp_path / "job-state",
        ZoneInfo("UTC"),
        lambda _: Summary("승인된 통화", "요약"),
    )
    job.run()
    assert [task for task, _ in llm.calls] == ["wiki.compile_batch"]
    assert store.read("Digest/2026/11/2026-11-01.md")


def make_job(tmp_path, store, fabric, llm):
    from zoneinfo import ZoneInfo

    from weaver.runtime.wiki import WikiJob

    return WikiJob(
        store,
        fabric,
        ContextDictionary(tmp_path / "job-context"),
        llm,
        tmp_path / "job-state",
        ZoneInfo("UTC"),
        lambda _: Summary("승인된 통화", "요약"),
    )


def test_invalid_digest_sentence_does_not_discard_pages(tmp_path, caplog):
    store, fabric, llm, _ = setup(tmp_path)

    def invalid(result):
        result["digests"][0]["sentences"].append(
            {"text": "secret body", "evidence": ["invalid secret"]}
        )
        return result

    llm.transform = invalid
    make_job(tmp_path, store, fabric, llm).run()
    assert store.read("pages/네트워크 운영.md")
    body = store.read("Digest/2026/11/2026-11-01.md")[1]
    assert "요약" in body and "secret" not in body
    assert "secret" not in caplog.text
    assert "digest sentence rejected" in caplog.text


def test_missing_digest_reappears_with_next_batch(tmp_path):
    store, fabric, llm, _ = setup(tmp_path)
    path = "Digest/2026/11/2026-11-01.md"
    llm.transform = lambda result: (
        result
        | {"digests": [entry for entry in result["digests"] if entry["path"] != path]}
    )
    job = make_job(tmp_path, store, fabric, llm)
    job.run()
    assert not (store.root / path).exists()
    job.run()
    assert len(llm.calls) == 1
    fabric.append([draft("three", ["네트워크"])])

    def include_new(result):
        result["pages"][0]["thread_ids"].append("three")
        return result

    llm.transform = include_new
    job.run()
    assert path in {t["path"] for t in llm.calls[-1][1]["digest_targets"]}
    assert store.read(path)
    assert len(llm.calls) == 2


def test_readonly_digest_edit_is_restored_without_blocking_pages(tmp_path):
    import os
    import time

    store, fabric, llm, _ = setup(tmp_path)
    job = make_job(tmp_path, store, fabric, llm)
    job.run()
    path = "Digest/2026/11/2026-11-01.md"
    for target in store.root.rglob("*.md"):
        os.utime(target, (time.time() - 2000, time.time() - 2000))
    digest = store.root / path
    digest.write_text(digest.read_text() + "\n사람 수정\n")
    store.settle_seconds = 1800
    fabric.append([draft("three", ["네트워크"])])

    def include_new(result):
        result["pages"][0]["thread_ids"].append("three")
        return result

    llm.transform = include_new
    job.run()
    assert job.compiler.bookmark() == 3
    assert "사람 수정" not in digest.read_text()
    # Restoration resets mtime, so the refreshed digest may wait for quiet time.
    assert path not in {t["path"] for t in llm.calls[-1][1]["digest_targets"]}


def test_batch_cache_reused_with_digest_after_interrupted_page_write(
    tmp_path, monkeypatch
):
    store, fabric, llm, _ = setup(tmp_path)
    job = make_job(tmp_path, store, fabric, llm)
    original = store.write

    def blocked(path, *args, **kwargs):
        if path == "pages/업무 도구.md":
            raise HumanEditPending(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(store, "write", blocked)
    job.run()
    assert job.compiler.bookmark() == 0 and len(llm.calls) == 1
    monkeypatch.setattr(store, "write", original)
    job.run()
    assert job.compiler.bookmark() == 2 and len(llm.calls) == 1
    assert store.read("Digest/2026/11/2026-11-01.md")


def test_batch_cache_survives_failure_after_digest_writes(tmp_path, monkeypatch):
    store, fabric, llm, _ = setup(tmp_path)
    job = make_job(tmp_path, store, fabric, llm)
    original = job.compiler._sync_pages

    def blocked():
        raise OSError("temporary sync failure")

    monkeypatch.setattr(job.compiler, "_sync_pages", blocked)
    with pytest.raises(OSError, match="temporary"):
        job.run()
    assert job.compiler.bookmark() == 0
    assert store.read("Digest/2026/11/2026-11-01.md")
    monkeypatch.setattr(job.compiler, "_sync_pages", original)
    job.run()
    assert len(llm.calls) == 1
    assert job.compiler.bookmark() == 2


def execution(key, executes, at):
    """A 실행 Thread: the result of an Action, not a business fact."""
    return ThreadDraft(
        thread_id=key,
        kind="실행",
        content="calendar.register completed",
        occurred_at=at,
        recorded_at=at,
        tags=[],
        field=None,
        value={
            "action_id": "act_" + "a" * 32,
            "action": "calendar.register",
            "outcome": "completed",
            "external_id": "content://calendar/events/7",
            "error": None,
        },
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        actor="system",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
        executes=executes,
    )


def test_executions_are_not_knowledge_and_trigger_no_new_batch(tmp_path):
    _, fabric, llm, compiler = setup(tmp_path)
    compiler.run()
    assert len(llm.calls) == 1 and compiler.bookmark() == 2
    fabric.append([execution("exe_1", "one", datetime(2026, 11, 2, tzinfo=UTC))])
    compiler.run()
    assert len(llm.calls) == 1
    assert compiler.bookmark() == 2
    assert all("exe_1" not in json.dumps(call[1]) for call in llm.calls)


def test_managed_evidence_is_not_fed_back_to_next_batch(tmp_path):
    store, fabric, llm, compiler = setup(tmp_path)
    compiler.run()
    assert "\n## 근거\n" in store.read("pages/네트워크 운영.md")[1]
    fabric.append([draft("three", ["네트워크"])])
    llm.transform = lambda data: data["pages"][0]["thread_ids"].append("three") or data
    compiler.run()
    existing = llm.calls[-1][1]["existing_pages"]
    assert existing and all("## 근거" not in page["body"] for page in existing)
    assert compiler.bookmark() == 3


def test_echoed_trailing_evidence_section_is_dropped_not_fatal(tmp_path):
    store, _, llm, compiler = setup(tmp_path)

    def echo(data):
        data["pages"][0]["body"] += "\n## 근거\n- [[summaries/옛 출처]]\n"
        return data

    llm.transform = echo
    compiler.run()
    body = store.read("pages/네트워크 운영.md")[1]
    assert body.count("## 근거") == 1
    assert "옛 출처" not in body
    assert compiler.bookmark() == 2


def test_evidence_heading_inside_body_is_still_rejected(tmp_path):
    store, _, llm, compiler = setup(tmp_path)

    def middle(data):
        body = data["pages"][0]["body"]
        data["pages"][0]["body"] = body + "\n## 근거\n- 수동\n\n## 다음\n내용\n"
        return data

    llm.transform = middle
    with pytest.raises(ValueError):
        compiler.run()
    assert compiler.bookmark() == 0


def test_area_derived_from_tree_path(tmp_path):
    from weaver.context.tree import expand

    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    fabric.append([draft("one", ["회사/infra/network/router"])])
    context = ContextDictionary(tmp_path / "context")
    context.sync_tree("회사", expand("회사", ["회사/infra/network/router"]))
    llm = Llm()
    llm.transform = lambda data: (
        data
        | {
            "pages": [
                {**p, "body": "# 네트워크 운영\n\n서술형 본문.\n"}
                for p in data["pages"]
            ]
        }
    )
    BatchCompiler(
        store,
        fabric,
        context,
        llm,
        tmp_path / "compiler.db",
        lambda _: Summary("승인된 통화", "요약 전문과 제약 조건"),
    ).run()
    props, _, _ = store.read("pages/네트워크 운영.md")
    assert props["area"] == "회사/infra/network"


def test_tree_subject_topic_is_a_promotion_grouping_key(tmp_path):
    from weaver.context.tree import expand

    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.sync_tree("회사", expand("회사", ["회사/infra/network/router"]))
    tag = ["회사/infra/network/router"]
    fabric.append([draft("a", tag), draft("b", tag)])
    new = fabric.append([draft("c", tag)])
    compiler = BatchCompiler(
        store,
        fabric,
        context,
        Llm(),
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    candidates = compiler._promotion_candidates(fabric.entries(ThreadQuery()), new)
    target = next(c for c in candidates if c["key"] == tag[0])
    assert target["area"] == "회사/infra/network"
    assert target["evidence_count"] == 3
