import os
import re
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from weaver.fabric import FabricStore, ThreadQuery
from weaver.wiki.checkbox import CheckboxWatcher
from weaver.wiki.digest import DigestBuilder
from weaver.wiki.snapshot import canonical_threads
from weaver.wiki.store import WikiStore

from .test_digest import draft, entries, plan, request

PATH = "Digest/2026/11/2026-11-20.md"
ZONE = ZoneInfo("Asia/Seoul")


def build(tmp_path, settle=0.0):
    fabric = FabricStore(tmp_path / "fabric.db")
    wiki = WikiStore(
        tmp_path / "vault",
        tmp_path / "wiki-state",
        settle_seconds=settle,
        clock=time.time,
    )
    builder = DigestBuilder(
        wiki, tmp_path / "digest.db", ZONE, lambda s: f"Events/{s}", lambda e: None
    )
    alerts = []
    watcher = CheckboxWatcher(
        wiki, fabric, tmp_path / "checkbox.db", ZONE, alerts.append
    )
    fabric.append(
        [
            request(
                "req", datetime(2026, 11, 20, 3, tzinfo=UTC), content="회의실 예약 확정"
            )
        ]
    )
    targets = plan(fabric, builder)
    builder.apply(targets, entries(targets), 1)
    return fabric, wiki, builder, watcher, alerts


def rows(fabric):
    return canonical_threads(fabric.entries(ThreadQuery()))


def checks(fabric):
    return [t for t in rows(fabric) if t.thread_id.startswith("wiki-check:")]


def test_tick_in_digest_records_resolve_once(tmp_path):
    fabric, wiki, builder, watcher, alerts = build(tmp_path)
    file = wiki.root / PATH
    file.write_text(
        re.sub(r"- \[ \] (.*?) (\^w-)", r"- [x] \1 ✅ 2026-11-21 \2", file.read_text())
    )
    result = watcher.collect(rows(fabric))
    (done,) = checks(fabric)
    assert done.resolves == "req" and done.actor == "human"
    assert done.occurred_at.astimezone(ZONE).date().isoformat() == "2026-11-21"
    assert any(t.thread_id == done.thread_id for t in result)
    watcher.collect(rows(fabric))
    assert len(checks(fabric)) == 1
    wiki.restore_readonly(on_change=watcher.on_change)
    assert "- [ ] 회의실 예약 확정" in file.read_text()
    assert alerts == []
    threads = rows(fabric)
    assert builder.refresh_tasks(threads, max(t.seq for t in threads)) == [PATH]
    assert re.search(r"- \[x\] 회의실 예약 확정 ✅ 2026-11-21 \^w-", file.read_text())


def test_conflict_copy_tick_counts_once_and_app_undo_is_not_reapplied(tmp_path):
    fabric, wiki, _, watcher, _ = build(tmp_path)
    original = wiki.root / PATH
    copy = original.with_name("2026-11-20.sync-conflict-20261121-ABCDEF.md")
    copy.write_text(original.read_text().replace("- [ ] 회의실", "- [x] 회의실"))
    watcher.collect(rows(fabric))
    (done,) = checks(fabric)
    watcher.collect(rows(fabric))
    assert len(checks(fabric)) == 1
    now = datetime(2026, 11, 22, tzinfo=UTC)
    fabric.append(
        [
            draft(
                "undo",
                now,
                kind="기록",
                content="되돌림",
                retracts=done.thread_id,
            )
        ]
    )
    watcher.collect(rows(fabric))
    assert len(checks(fabric)) == 1
    assert copy.exists() and "- [x] 회의실" in copy.read_text()


def test_checkbox_only_change_no_alert(tmp_path):
    fabric, wiki, _, watcher, alerts = build(tmp_path)
    file = wiki.root / PATH
    file.write_text(file.read_text().replace("- [ ]", "- [x]"))
    wiki.restore_readonly(on_change=watcher.on_change)
    assert alerts == []
    assert "- [ ] 회의실" in file.read_text()


def test_other_edit_alerts_once_then_restores_after_settle(tmp_path):
    fabric, wiki, _, watcher, alerts = build(tmp_path, settle=1800)
    file = wiki.root / PATH
    baseline = file.read_text()
    edited = baseline.replace("요약", "고친 요약")
    file.write_text(edited)
    os.utime(file, (time.time() - 10,) * 2)
    wiki.restore_readonly(on_change=watcher.on_change)
    assert file.read_text() == edited and alerts == []
    os.utime(file, (time.time() - 3600,) * 2)
    wiki.restore_readonly(on_change=watcher.on_change)
    assert file.read_text() == baseline
    (alert,) = alerts
    assert "피드백 탭으로 보내 주세요" in alert.message
    assert alert.package_id.startswith("wiki:")
    file.write_text(edited)
    os.utime(file, (time.time() - 3600,) * 2)
    wiki.restore_readonly(on_change=watcher.on_change)
    assert file.read_text() == baseline
    assert len(alerts) == 1


def test_block_for_unknown_or_resolved_request_ignored(tmp_path):
    fabric, wiki, _, watcher, _ = build(tmp_path)
    file = wiki.root / PATH
    file.write_text(
        file.read_text()
        + "- [x] 없는 요청 ^w-0123456789\n"
        + "- [x] 다른 요청 ^w-abcdef0123\n"
    )
    watcher.collect(rows(fabric))
    assert checks(fabric) == []
    fabric.append(
        [
            draft(
                "done",
                datetime(2026, 11, 21, tzinfo=UTC),
                kind="결정",
                resolves="req",
            )
        ]
    )
    file.write_text(file.read_text().replace("- [ ] 회의실", "- [x] 회의실"))
    watcher.collect(rows(fabric))
    assert checks(fabric) == []


def _complete_and_render(fabric, builder, wiki):
    fabric.append(
        [
            draft(
                "app-done",
                datetime(2026, 11, 21, 3, tzinfo=UTC),
                kind="결정",
                resolves="req",
            )
        ]
    )
    threads = rows(fabric)
    builder.refresh_tasks(threads, max(t.seq for t in threads))
    assert "- [x] 회의실" in (wiki.root / PATH).read_text()


def _undo(fabric):
    fabric.append(
        [
            draft(
                "app-undo",
                datetime(2026, 11, 22, 3, tzinfo=UTC),
                content="되돌림",
                retracts="app-done",
            )
        ]
    )


def test_app_complete_render_undo_is_not_a_human_tick(tmp_path):
    fabric, wiki, builder, watcher, _ = build(tmp_path)
    _complete_and_render(fabric, builder, wiki)
    _undo(fabric)
    watcher.collect(rows(fabric))
    assert checks(fabric) == []


def test_conflict_copy_taken_while_done_does_not_recomplete_after_undo(tmp_path):
    fabric, wiki, builder, watcher, _ = build(tmp_path)
    _complete_and_render(fabric, builder, wiki)
    file = wiki.root / PATH
    copy = file.with_name("2026-11-20.sync-conflict-20261121-ABCDEF.md")
    copy.write_text(file.read_text())
    watcher.collect(rows(fabric))  # next job start sees the rendered [x]
    _undo(fabric)
    threads = rows(fabric)
    builder.refresh_tasks(threads, max(t.seq for t in threads))
    watcher.collect(rows(fabric))
    assert checks(fabric) == []


def test_alert_failure_defers_restore_and_retries_deterministically(tmp_path):
    fabric, wiki, _, watcher, _ = build(tmp_path)
    bodies = []

    def flaky(alert):
        bodies.append(alert)
        if len(bodies) == 1:
            raise RuntimeError("transient")

    watcher.publish_alert = flaky
    file = wiki.root / PATH
    baseline = file.read_text()
    edited = baseline.replace("요약", "고친 요약")
    file.write_text(edited)
    wiki.restore_readonly(on_change=watcher.on_change)
    assert file.read_text() == edited
    time.sleep(0.01)
    wiki.restore_readonly(on_change=watcher.on_change)
    assert file.read_text() == baseline
    assert len(bodies) == 2 and bodies[0] == bodies[1]


def test_fresh_tick_after_app_undo_and_rerender_is_recorded(tmp_path):
    fabric, wiki, builder, watcher, _ = build(tmp_path)
    _complete_and_render(fabric, builder, wiki)
    watcher.collect(rows(fabric))  # next job start sees the rendered [x]
    _undo(fabric)
    threads = rows(fabric)
    builder.refresh_tasks(threads, max(t.seq for t in threads))
    file = wiki.root / PATH
    assert "- [ ] 회의실" in file.read_text()
    file.write_text(file.read_text().replace("- [ ] 회의실", "- [x] 회의실"))
    watcher.collect(rows(fabric))
    assert len(checks(fabric)) == 1


def test_staged_alert_publisher_reuses_staged_body(tmp_path):
    from weaver.alert.history import AlertHistory
    from weaver.runtime.assemble import staged_alert_publisher
    from weaver.schema import Alert

    class Delivery:
        def __init__(self):
            self.history = AlertHistory(tmp_path / "alerts.db")
            self.sent = []

        def publish_alert(self, alert):
            if self.history.body(alert.alert_id) != alert:
                raise ValueError("alert_not_staged")
            self.sent.append(alert)

    delivery = Delivery()
    publish = staged_alert_publisher(delivery)
    first = Alert(
        schema_version=2,
        alert_id="alt_" + "0" * 26,
        package_id="wiki:abc",
        thread_id="wiki-overwrite",
        message="첫 메시지",
        created_at=datetime(2026, 11, 22, tzinfo=UTC),
    )
    publish(first)
    changed = first.model_copy(update={"message": "바뀐 메시지"})
    publish(changed)
    assert delivery.sent == [first, first]


def tick(file, date="2026-11-21"):
    file.write_text(
        re.sub(r"- \[ \] (.*?) (\^w-)", rf"- [x] \1 ✅ {date} \2", file.read_text())
    )


def test_obsidian_tick_app_undo_then_fresh_tick_records_new_completion(tmp_path):
    fabric, wiki, builder, watcher, _ = build(tmp_path)
    file = wiki.root / PATH
    original = file.read_text()
    tick(file)
    watcher.collect(rows(fabric))
    (first,) = checks(fabric)
    wiki.restore_readonly(on_change=watcher.on_change)
    threads = rows(fabric)
    builder.refresh_tasks(threads, max(t.seq for t in threads))
    assert "- [x] 회의실 예약 확정" in file.read_text()
    fabric.append(
        [
            draft(
                "undo",
                datetime(2026, 11, 22, tzinfo=UTC),
                kind="기록",
                content="되돌림",
                retracts=first.thread_id,
            )
        ]
    )
    # A stale [x] before the page is re-rendered is not a new tick.
    watcher.collect(rows(fabric))
    assert len(checks(fabric)) == 1
    threads = rows(fabric)
    builder.refresh_tasks(threads, max(t.seq for t in threads))
    assert "- [ ] 회의실 예약 확정" in file.read_text()
    assert original.count("- [ ]") == file.read_text().count("- [ ]")
    tick(file, "2026-11-23")
    watcher.collect(rows(fabric))
    both = checks(fabric)
    assert len(both) == 2 and both[0].thread_id != both[1].thread_id
    assert both[1].resolves == "req" and both[1].source_id != both[0].source_id


def test_stale_conflict_copy_cannot_record_again_after_undo(tmp_path):
    fabric, wiki, builder, watcher, _ = build(tmp_path)
    original = wiki.root / PATH
    copy = original.with_name("2026-11-20.sync-conflict-20261121-ABCDEF.md")
    copy.write_text(original.read_text().replace("- [ ] 회의실", "- [x] 회의실"))
    watcher.collect(rows(fabric))
    (first,) = checks(fabric)
    fabric.append(
        [
            draft(
                "undo",
                datetime(2026, 11, 22, tzinfo=UTC),
                kind="기록",
                content="되돌림",
                retracts=first.thread_id,
            )
        ]
    )
    threads = rows(fabric)
    builder.refresh_tasks(threads, max(t.seq for t in threads))
    watcher.collect(rows(fabric))
    assert len(checks(fabric)) == 1


def test_wiki_job_survives_checkbox_failure(tmp_path):
    from types import SimpleNamespace

    from weaver.runtime.wiki import WikiJob

    calls = []

    class Boom:
        def collect(self, rows):
            raise RuntimeError("bad")

        on_change = None

    class Stop(Exception):
        pass

    def restore(on_change=None):
        calls.append("restore")
        raise Stop

    job = SimpleNamespace(
        watcher=Boom(),
        fabric=SimpleNamespace(entries=lambda q: []),
        store=SimpleNamespace(restore_readonly=restore),
    )
    try:
        WikiJob._run(job)
    except Stop:
        pass
    assert calls == ["restore"]
