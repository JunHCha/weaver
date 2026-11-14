"""Retry consumers without real models, clocks, or vaults."""

import json
import logging
import os
import sqlite3
import unicodedata
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore
from weaver.runtime.wiki import WikiJob
from weaver.schema import ThreadDraft
from weaver.wiki.compiler import Compiler, EditResult, Summary
from weaver.wiki.digest import DigestBuilder
from weaver.wiki.store import HumanEditPending, WikiStore


class Llm:
    def __init__(self):
        self.calls = []
        self.hook = lambda: None

    def generate_text(self, task, system, user):
        self.calls.append((task, user))
        self.hook()
        return "generated " + user

    def generate_json(self, task, system, user, schema):
        if task == "wiki.human_edit":
            return EditResult(changes=[])
        self.calls.append((task, user))
        self.hook()
        return schema.model_validate(
            {
                "sentences": [
                    {
                        "text": "generated " + json.dumps(json.loads(user)["inputs"]),
                        "evidence": json.loads(user)["evidence"],
                    }
                ]
            }
        )


def thread(key, tags=None):
    return ThreadDraft(
        thread_id=key,
        kind="기록",
        content=key,
        occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        recorded_at=datetime(2025, 1, 1, tzinfo=UTC),
        tags=tags or ["topic"],
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        actor="human",
        field=None,
        value=None,
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )


@pytest.fixture(params=["digest", "compiler"])
def consumer(tmp_path, request):
    vault = tmp_path / "vault"
    vault.mkdir()
    clock = [2_000_000_000.0]
    store = WikiStore(
        vault, tmp_path / "wiki", clock=lambda: clock[0], settle_seconds=1800
    )
    fabric = FabricStore(tmp_path / "fabric.db")
    llm = Llm()
    context = ContextDictionary(tmp_path / "context")
    state = tmp_path / "consumer.db"

    def make():
        if request.param == "digest":
            return DigestBuilder(
                fabric, store, llm, state, ZoneInfo("UTC"), lambda s: s
            )
        return Compiler(
            store, fabric, context, llm, state, lambda _: Summary("source", "summary")
        )

    def run(instance):
        if request.param == "digest":
            instance.update()
        else:
            instance.run()

    def bookmark():
        with sqlite3.connect(state) as db:
            row = db.execute("SELECT seq FROM bookmark").fetchone()
        return row[0] if row else 0

    path = (
        "Digest/2025/01/2025-01-01.md"
        if request.param == "digest"
        else "pages/topic.md"
    )
    return store, fabric, llm, clock, make, run, bookmark, path


def stamp(store, now):
    for target in store.root.rglob("*.md"):
        os.utime(target, (now, now))


def test_settle_blocks_generation_across_cycles_and_then_updates(consumer):
    store, fabric, llm, clock, make, run, bookmark, path = consumer
    fabric.append([thread("first")])
    run(make())
    stamp(store, clock[0])
    llm.calls.clear()
    fabric.append([thread("second")])
    original = store.read(path)[2]
    for _ in range(5):
        clock[0] += 60
        with pytest.raises(HumanEditPending):
            run(make())
        assert bookmark() == 1
    assert llm.calls == []
    assert store.read(path)[2] == original
    clock[0] += 1800
    run(make())
    # Each digest level generates once; the compiler has one page.
    assert len(llm.calls) == (3 if path.startswith("Digest") else 1)
    assert bookmark() == 2
    assert store.read(path)[2] != original
    run(make())
    assert len(llm.calls) == (3 if path.startswith("Digest") else 1)


def test_generation_race_reuses_durable_result_after_settle(consumer):
    store, fabric, llm, clock, make, run, bookmark, path = consumer
    fabric.append([thread("first")])
    run(make())
    stamp(store, clock[0] - 1800)
    llm.calls.clear()
    fabric.append([thread("second")])
    target = store.root / path
    llm.hook = lambda: os.utime(target, (clock[0], clock[0]))
    with pytest.raises(HumanEditPending):
        run(make())
    assert len(llm.calls) == 1
    assert bookmark() == 1
    llm.hook = lambda: None
    for _ in range(4):
        clock[0] += 60
        with pytest.raises(HumanEditPending):
            run(make())
    assert len(llm.calls) == 1
    clock[0] += 1800
    run(make())
    assert len([c for c in llm.calls if c == llm.calls[0]]) == 1
    assert bookmark() == 2


def test_changed_input_invalidates_race_cache(consumer):
    store, fabric, llm, clock, make, run, bookmark, path = consumer
    fabric.append([thread("first")])
    run(make())
    stamp(store, clock[0] - 1800)
    llm.calls.clear()
    target = store.root / path
    fabric.append([thread("second")])
    llm.hook = lambda: os.utime(target, (clock[0], clock[0]))
    with pytest.raises(HumanEditPending):
        run(make())
    fabric.append([thread("third")])
    llm.hook = lambda: None
    clock[0] += 1800
    run(make())
    assert len(llm.calls) == (4 if path.startswith("Digest") else 2)
    assert "third" in llm.calls[1][1]
    assert bookmark() == 3


def test_deferred_logging_is_private_and_only_on_state_change(tmp_path, caplog):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = WikiStore(vault, tmp_path / "wiki", clock=lambda: 1000)
    job = WikiJob(
        store,
        FabricStore(tmp_path / "fabric.db"),
        ContextDictionary(tmp_path / "context"),
        Llm(),
        tmp_path,
        ZoneInfo("UTC"),
        lambda _: Summary("source", "summary"),
    )
    error = HumanEditPending("private-page-name", ready_at=1600)

    def pending():
        raise error

    job.compiler.run = pending
    with caplog.at_level(logging.INFO):
        job.run()
        job.run()
        error.ready_at = 1700
        job.run()
        job.compiler.run = lambda: None
        job.run()
        job.compiler.run = pending
        job.run()
    messages = [
        r.getMessage() for r in caplog.records if "consumer=compiler" in r.getMessage()
    ]
    assert len(messages) == 3
    assert all("private-page-name" not in m for m in messages)
    assert "count=1 remaining_seconds=600" in messages[0]


def test_partial_failure_preserves_completed_pages_and_bookmark(consumer, monkeypatch):
    store, fabric, llm, clock, make, run, bookmark, path = consumer
    fabric.append([thread("first", ["a", "b"])])
    blocked = "Digest/Weekly/2025-W01.md" if path.startswith("Digest") else "pages/b.md"
    original_write = store.write

    def write(page, *args, **kwargs):
        if page == blocked:
            raise HumanEditPending(page)
        return original_write(page, *args, **kwargs)

    monkeypatch.setattr(store, "write", write)
    for _ in range(3):
        with pytest.raises(HumanEditPending):
            run(make())
        stamp(store, clock[0])
        assert bookmark() == 0
    assert len(llm.calls) == 2
    monkeypatch.setattr(store, "write", original_write)
    run(make())
    assert len(llm.calls) == (3 if path.startswith("Digest") else 2)
    assert bookmark() == 1


def test_preflight_hash_mismatch_does_not_modify_page(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = WikiStore(vault, tmp_path / "wiki", settle_seconds=0)
    base = store.write("page.md", {}, "initial", None)
    target = store.root / "page.md"
    target.write_text("human content")
    with pytest.raises(HumanEditPending):
        store.check_write("page.md", base)
    with pytest.raises(HumanEditPending):
        store.write("page.md", {}, "generated", base)
    assert target.read_text() == "human content"


def test_decomposed_summary_links_and_page_names_across_consumers(tmp_path):
    clock = [2_000_000_000.0]
    store = WikiStore(tmp_path / "vault", tmp_path / "wiki", clock=lambda: clock[0])
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    title = "한글 요약"
    nfd_title = unicodedata.normalize("NFD", title)

    class Lookup:
        def title(self, source_id):
            return nfd_title

        def __call__(self, source_id):
            return Summary(nfd_title, "summary")

    class NoHumanLlm(Llm):
        def generate_json(self, task, system, user, schema):
            assert task != "wiki.human_edit"
            if task == "wiki.compile_batch":
                self.calls.append((task, user))
                payload = json.loads(user)
                return schema.model_validate(
                    {
                        "pages": [
                            {
                                "title": "계정잠금",
                                "role": "operation",
                                "summary": "계정잠금 운영",
                                "thread_ids": [
                                    t["thread_id"] for t in payload["threads"]
                                ],
                                "state_tag": None,
                                "aliases": [],
                                "body": "# 계정잠금\n\n운영 현황",
                            }
                        ],
                        "excluded_threads": [],
                    }
                )
            return super().generate_json(task, system, user, schema)

    llm = NoHumanLlm()
    item = thread("first", ["계정잠금"]).model_copy(update={"actor": "ai_approved"})
    fabric.append([item])
    job = WikiJob(store, fabric, context, llm, tmp_path, ZoneInfo("UTC"), Lookup())
    job.run()
    link = f"summaries/{title} {item.source_id}"
    assert job.source_link(item.source_id) == link
    assert f"[[{link}]]" in store.read("pages/계정잠금.md")[1]
    assert f"[[{link}]]" in store.read("Digest/2025/01/2025-01-01.md")[1]

    # Force the spelling returned by directory enumeration on both platforms.
    for target in list(store.root.rglob("*.md")):
        target.rename(target.with_name(unicodedata.normalize("NFD", target.name)))
    stamp(store, clock[0])
    calls = len(llm.calls)
    for _ in range(3):
        clock[0] += 1801
        job.run()
    assert len(llm.calls) == calls
    assert context.candidates("계정잠금")[0].name == "계정잠금"
    with sqlite3.connect(store.db_path) as db:
        paths = [row[0] for row in db.execute("SELECT path FROM pages")]
        assert all(unicodedata.is_normalized("NFC", path) for path in paths)
        assert len(paths) == len(set(paths))
