"""Whole-corpus generation with fake models and isolated temporary vaults."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest

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
            return schema.model_validate({"changes": []})
        pages = []
        for title, ids in [("네트워크 운영", ["one"]), ("업무 도구", ["two"])]:
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
            self.transform({"pages": pages, "excluded_threads": []})
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
    store.write("pages/네트워크 운영.md", props, body + "사람 표현 유지\n", base)
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
