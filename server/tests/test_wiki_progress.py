"""Wiki edits begin at Wiki; observations never manufacture a capture/review."""

import json

import pytest

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.runtime.progress import PipelineJournal
from weaver.runtime.wiki_progress import WikiEditPublisher
from weaver.wiki.batch import BatchCompiler
from weaver.wiki.compiler import EditResult
from weaver.wiki.store import WikiStore


class Client:
    fail = True
    formatting_only = False

    def generate_json(self, task, system, user, schema):
        if task == "wiki.human_edit":
            return EditResult.model_validate(
                {
                    "changes": []
                    if self.formatting_only
                    else [{"content": "현장 확인 후 진행", "tags": ["점검"]}]
                }
            )
        if self.fail:
            raise RuntimeError("generation paused")
        payload = json.loads(user)
        return schema.model_validate(
            {
                "pages": [
                    {
                        "title": "운영",
                        "role": "operation",
                        "summary": "점검 운영 현황",
                        "state_tag": "점검",
                        "aliases": [],
                        "thread_ids": [t["thread_id"] for t in payload["threads"]],
                        "body": "# 운영\n\n현장 확인 후 진행한다.",
                    }
                ],
                "excluded_threads": [],
                "digests": [],
            }
        )


def setup(tmp_path):
    clock = [2_000_000_000.0]
    store = WikiStore(
        tmp_path / "vault",
        tmp_path / "state",
        settle_seconds=10,
        clock=lambda: clock[0],
    )
    store.write(
        "pages/운영.md",
        {"kind": "topic", "state_tag": "점검", "deadline": "2026-11-16"},
        "# 운영\n\n원문",
        None,
    )
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    client = Client()

    def no_summary(_):
        raise LookupError("no source summary for a direct edit")

    compiler = BatchCompiler(
        store,
        fabric,
        ContextDictionary(tmp_path / "state"),
        client,
        tmp_path / "compiler.sqlite3",
        no_summary,
    )
    journal = PipelineJournal(tmp_path / "state", tmp_path / "proposals")
    publisher = WikiEditPublisher(journal, store, compiler, fabric, tmp_path / "vault")
    compiler.on_progress = publisher.publish
    return clock, store, fabric, client, compiler, journal, publisher


def test_wait_record_publish_and_restart_preserve_reverse_history(tmp_path):
    clock, store, fabric, client, compiler, journal, publisher = setup(tmp_path)
    page = store.root / "pages/운영.md"
    page.write_text(
        page.read_text().replace('"2026-11-16"', '"2026-11-19"')
        + "\n현장 확인 후 진행\n"
    )
    compiler.run()
    publisher.publish()
    waiting = journal.wiki_edits()[0]
    assert waiting.capture_id is None and waiting.package_id is None
    assert waiting.source_type == "wiki_edit" and waiting.history == ["wiki"]
    assert (
        waiting.wiki_edit.state == "settling" and waiting.wiki_edit.ready_at is not None
    )
    assert waiting.stage_times == {} and waiting.threads == []
    clock[0] += 11
    with pytest.raises(RuntimeError, match="paused"):
        compiler.run()
    recorded = journal.get(waiting.source_id)
    assert recorded.history == ["wiki", "fabric"]
    assert recorded.wiki_edit.state == "recorded" and len(recorded.threads) == 2
    assert "반영" in recorded.stage_times and "wiki갱신" not in recorded.stage_times
    client.fail = False
    clock[0] += 11
    compiler.run()
    publisher.publish()
    complete = journal.get(waiting.source_id)
    assert complete.history == ["wiki", "fabric", "wiki"]
    assert complete.status == "done" and complete.wiki_edit.state == "published"
    assert {p.path for p in complete.pages} == {"Weaver/pages/운영.md"}
    assert len(complete.wiki_edit.changes) == 2
    assert complete.wiki_edit.changes[0].before == "2026-11-16"
    assert complete.wiki_edit.changes[0].after == "2026-11-19"
    assert set(complete.stage_times) == {"반영", "wiki갱신"}
    assert all(t.content for t in complete.threads)
    assert len(fabric.entries(ThreadQuery())) == 2
    restarted = WikiEditPublisher(
        PipelineJournal(tmp_path / "state", tmp_path / "proposals"),
        store,
        compiler,
        fabric,
        tmp_path / "vault",
    )
    restarted.publish()
    assert journal.get(waiting.source_id).revision == complete.revision
    raw = json.loads(next((tmp_path / "proposals/pipeline").glob("*.json")).read_text())
    assert raw["capture_id"] is None and raw["wiki_edit"]["state"] == "published"


def test_more_typing_updates_one_waiting_event_and_revert_closes_it(tmp_path):
    clock, store, _, _, _, journal, publisher = setup(tmp_path)
    page = store.root / "pages/운영.md"
    baseline = page.read_bytes()
    page.write_bytes(baseline + b"\nfirst")
    store.scan(clock[0])
    publisher.publish()
    first = journal.wiki_edits()[0]
    clock[0] += 2
    page.write_bytes(baseline + b"\nsecond")
    store.scan(clock[0])
    publisher.publish()
    assert len(journal.wiki_edits()) == 1
    assert journal.wiki_edits()[0].source_id == first.source_id
    assert journal.wiki_edits()[0].wiki_edit.ready_at > first.wiki_edit.ready_at
    page.write_bytes(baseline)
    store.scan(clock[0])
    publisher.publish()
    reverted = journal.get(first.source_id)
    assert reverted.wiki_edit.state == "unchanged"
    assert reverted.history == ["wiki"] and not reverted.threads


def test_formatting_only_edit_is_not_shown_as_a_fabric_transition(tmp_path):
    clock, store, fabric, client, compiler, journal, publisher = setup(tmp_path)
    client.formatting_only = True
    page = store.root / "pages/운영.md"
    page.write_text(page.read_text() + "\n\n")
    compiler.run()
    publisher.publish()
    clock[0] += 11
    compiler.run()
    publisher.publish()
    item = journal.wiki_edits()[0]
    assert item.status == "done" and item.wiki_edit.state == "unchanged"
    assert item.history == ["wiki"] and item.stage_times == {}
    assert fabric.entries(ThreadQuery()) == []


def test_failed_append_is_not_advertised_as_a_fabric_commit(tmp_path, monkeypatch):
    clock, store, fabric, client, compiler, journal, publisher = setup(tmp_path)
    page = store.root / "pages/운영.md"
    page.write_text(page.read_text() + "\n현장 확인 후 진행\n")
    compiler.run()
    publisher.publish()
    clock[0] += 11
    original = fabric.append

    def unavailable(_):
        raise OSError("unavailable")

    monkeypatch.setattr(fabric, "append", unavailable)
    with pytest.raises(OSError):
        compiler.run()
    publisher.publish()
    waiting = journal.wiki_edits()[0]
    assert waiting.history == ["wiki"] and not waiting.threads
    assert "반영" not in waiting.stage_times
    monkeypatch.setattr(fabric, "append", original)
    client.fail = False
    compiler.run()
    publisher.publish()
    assert journal.wiki_edits()[0].history == ["wiki", "fabric", "wiki"]
    assert len(fabric.entries(ThreadQuery())) == 1


def test_observation_failure_never_rolls_back_commit_and_is_recovered(
    tmp_path, monkeypatch
):
    clock, store, fabric, client, compiler, journal, publisher = setup(tmp_path)
    page = store.root / "pages/운영.md"
    page.write_text(page.read_text() + "\n현장 확인 후 진행\n")
    compiler.run()
    clock[0] += 11
    client.fail = False
    original = journal.receive_wiki_edit

    def unavailable(_):
        raise OSError("projection unavailable")

    monkeypatch.setattr(journal, "receive_wiki_edit", unavailable)
    compiler.run()
    assert len(fabric.entries(ThreadQuery())) == 1
    assert compiler.bookmark() == 1
    monkeypatch.setattr(journal, "receive_wiki_edit", original)
    publisher.publish()
    assert journal.wiki_edits()[0].history == ["wiki", "fabric", "wiki"]
