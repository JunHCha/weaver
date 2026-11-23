import re
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from weaver.fabric import FabricStore, ThreadQuery
from weaver.schema import ThreadDraft
from weaver.wiki.digest import DigestBuilder, DigestEntry, card_texts
from weaver.wiki.snapshot import canonical_threads
from weaver.wiki.store import WikiStore

SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def draft(key, at, **changes):
    data = dict(
        thread_id=key,
        kind="기록",
        content=key,
        occurred_at=at,
        recorded_at=at,
        tags=["홍길동"],
        field=None,
        value=None,
        source_id=SOURCE,
        actor="human",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )
    data.update(changes)
    return ThreadDraft.model_validate(data)


def setup(tmp_path):
    fabric = FabricStore(tmp_path / "fabric.db")
    wiki = WikiStore(tmp_path / "vault", tmp_path / "wiki-state", settle_seconds=0)
    deferred = []
    builder = DigestBuilder(
        wiki,
        tmp_path / "digest.db",
        ZoneInfo("Asia/Seoul"),
        lambda source: f"Events/{source}",
        deferred.append,
    )
    return fabric, wiki, builder, deferred


def plan(fabric, builder):
    threads = canonical_threads(fabric.entries(ThreadQuery()))
    return builder.plan(threads, max(t.seq for t in threads))


def entries(targets):
    return [
        DigestEntry.model_validate(
            {
                "path": t.path,
                "sentences": [{"text": "요약", "evidence": t.evidence}]
                if t.evidence
                else [],
            }
        )
        for t in targets
    ]


def test_late_arrival_regenerates_changed_periods_and_skips_empty(tmp_path):
    fabric, wiki, builder, _ = setup(tmp_path)
    fabric.append([draft("first", datetime(2025, 1, 10, tzinfo=UTC))])
    targets = plan(fabric, builder)
    assert len(targets) == 3
    builder.apply(targets, entries(targets), 1)
    assert (
        f"[[Events/{SOURCE}|{SOURCE}]]" in wiki.read("Digest/2025/01/2025-01-10.md")[1]
    )
    assert plan(fabric, builder) == []
    fabric.append([draft("late", datetime(2025, 1, 9, tzinfo=UTC))])
    targets = plan(fabric, builder)
    assert len(targets) == 3
    builder.apply(targets, entries(targets), 2)
    assert not (wiki.root / "Digest/2025/01/2025-01-08.md").exists()


def test_schedule_value_day_is_not_a_digest_day(tmp_path):
    fabric, wiki, builder, _ = setup(tmp_path)
    at = datetime(2025, 1, 1, tzinfo=UTC)
    fabric.append(
        [
            draft(
                "old",
                at,
                field="schedule",
                value={"start": "2025-02-03T00:00:00Z", "end": None, "timezone": "UTC"},
            )
        ]
    )
    targets = plan(fabric, builder)
    paths = {t.path for t in targets}
    assert paths == {
        "Digest/2025/01/2025-01-01.md",
        "Digest/Weekly/2025-W01.md",
        "Digest/Monthly/2025-01.md",
    }
    builder.apply(targets, entries(targets), 1)
    assert not (wiki.root / "Digest/2025/02").exists()


def request(key, at, **changes):
    data = dict(kind="요청", direction="받음")
    data.update(changes)
    return draft(key, at, **data)


def test_future_deadline_never_creates_future_digest(tmp_path):
    fabric, wiki, builder, _ = setup(tmp_path)
    fabric.append(
        [
            request(
                "req",
                datetime(2026, 11, 20, 3, tzinfo=UTC),
                field="deadline",
                value="2026-12-05",
            )
        ]
    )
    targets = plan(fabric, builder)
    daily = [t.path for t in targets if t.period == "daily"]
    assert daily == ["Digest/2026/11/2026-11-20.md"]
    builder.apply(targets, entries(targets), 1)
    assert not list(wiki.root.rglob("2026-12-05.md"))
    body = wiki.read("Digest/2026/11/2026-11-20.md")[1]
    assert "\n# 2026-11-20 (금)\n\n## 들어온 일\n" in body
    assert re.search(r"- \[ \] req 📅 2026-12-05 \^w-[0-9a-f]{10}\n", body)


def test_resolved_request_renders_checked_without_llm(tmp_path):
    fabric, wiki, builder, _ = setup(tmp_path)
    fabric.append([request("req", datetime(2026, 11, 20, 3, tzinfo=UTC))])
    targets = plan(fabric, builder)
    builder.apply(targets, entries(targets), 1)
    path = "Digest/2026/11/2026-11-20.md"
    before = wiki.read(path)[1]
    assert re.search(r"- \[ \] req \^w-", before)
    fabric.append(
        [
            draft(
                "done",
                datetime(2026, 11, 21, 3, tzinfo=UTC),
                kind="결정",
                resolves="req",
            )
        ]
    )
    threads = canonical_threads(fabric.entries(ThreadQuery()))
    changed = builder.refresh_tasks(threads, max(t.seq for t in threads))
    assert changed == [path]
    after = wiki.read(path)[1]
    assert re.search(r"- \[x\] req ✅ 2026-11-21 \^w-[0-9a-f]{10}\n", after)
    assert after.split("## 요약")[1] == before.split("## 요약")[1]
    assert builder.refresh_tasks(threads, max(t.seq for t in threads)) == []


def test_cards_validated_and_stored(tmp_path, caplog):
    fabric, wiki, builder, _ = setup(tmp_path)
    fabric.append([draft("first", datetime(2025, 1, 1, tzinfo=UTC))])
    targets = plan(fabric, builder)
    day = next(t for t in targets if t.period == "daily")
    keys = [c["key"] for c in day.inputs["cards"]]
    assert keys == ["source:" + SOURCE]
    response = DigestEntry.model_validate(
        {
            "path": day.path,
            "sentences": [{"text": "요약", "evidence": [SOURCE]}],
            "cards": [
                {
                    "key": keys[0],
                    "headline": "회의실 예약 시스템 교체",
                    "bullets": ["견적 대기"],
                },
                {"key": "unknown", "headline": "없는 대상", "bullets": []},
            ],
        }
    )
    with caplog.at_level("WARNING"):
        others = [e for e in entries(targets) if e.path != day.path]
        builder.apply(targets, [response, *others], 1)
    stored = card_texts(builder.state_path)
    assert list(stored) == [("2025-01-01", keys[0])]
    assert stored["2025-01-01", keys[0]].headline == "회의실 예약 시스템 교체"
    assert stored["2025-01-01", keys[0]].bullets == ["견적 대기"]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert "없는 대상" not in caplog.text


def test_one_line_per_request(tmp_path):
    fabric, wiki, builder, _ = setup(tmp_path)
    at = datetime(2026, 11, 20, 3, tzinfo=UTC)
    fabric.append([request("a", at), request("b", at, direction="보냄")])
    targets = plan(fabric, builder)
    builder.apply(targets, entries(targets), 1)
    body = wiki.read("Digest/2026/11/2026-11-20.md")[1]
    lines = [line for line in body.splitlines() if line.startswith("- [ ]")]
    assert len(lines) == 2
    assert len({line.rsplit("^", 1)[1] for line in lines}) == 2


def test_partial_sentence_validation_and_missing_target(tmp_path, caplog):
    fabric, wiki, builder, _ = setup(tmp_path)
    fabric.append([draft("first", datetime(2025, 1, 1, tzinfo=UTC))])
    targets = plan(fabric, builder)
    day = next(t for t in targets if t.period == "daily")
    response = DigestEntry.model_validate(
        {
            "path": day.path,
            "sentences": [
                {"text": "valid", "evidence": [SOURCE]},
                {"text": "private bad", "evidence": ["unknown"]},
                {"text": "newline\nprivate", "evidence": [SOURCE]},
                {"text": "no evidence", "evidence": []},
            ],
        }
    )
    builder.apply(targets, [response], 1)
    body = wiki.read(day.path)[1]
    assert "valid" in body and "private" not in body and "no evidence" not in body
    assert "private" not in caplog.text and "unknown" not in caplog.text
    assert {t.path for t in plan(fabric, builder)} == {
        t.path for t in targets if t != day
    }


def test_human_edit_deferred_without_preventing_other_targets(tmp_path):
    fabric, wiki, builder, deferred = setup(tmp_path)
    at = datetime(2025, 1, 1, tzinfo=UTC)
    fabric.append([draft("first", at)])
    targets = plan(fabric, builder)
    builder.apply(targets, entries(targets), 1)
    path = "Digest/2025/01/2025-01-01.md"
    target = wiki.root / path
    target.write_text(target.read_text() + "\n사람 수정\n")
    fabric.append([draft("next", at)])
    wiki.settle_seconds = 999999
    targets = plan(fabric, builder)
    assert path not in {t.path for t in targets}
    assert deferred
    wiki.settle_seconds = 0
    targets = plan(fabric, builder)
    assert "사람 수정" in next(t.previous for t in targets if t.path == path)


def test_request_with_deadline_appears_once_without_source_link(tmp_path):
    fabric, wiki, builder, _ = setup(tmp_path)
    builder.source_link = lambda _: None
    fabric.append(
        [
            draft(
                "request",
                datetime(2025, 1, 1, tzinfo=UTC),
                kind="요청",
                direction="받음",
                field="deadline",
                value="2025-01-01",
            )
        ]
    )
    targets = plan(fabric, builder)
    builder.apply(targets, entries(targets), 1)
    body = wiki.read("Digest/2025/01/2025-01-01.md")[1]
    assert body.split("## 요약")[0].count("request") == 1
    assert "[[" not in body


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


def test_digest_never_plans_a_day_for_an_execution(tmp_path):
    fabric, _, builder, _ = setup(tmp_path)
    fabric.append([draft("first", datetime(2025, 1, 10, tzinfo=UTC))])
    targets = plan(fabric, builder)
    builder.apply(targets, entries(targets), 1)
    fabric.append([execution("exe_1", "first", datetime(2025, 3, 5, tzinfo=UTC))])
    assert plan(fabric, builder) == []
    # Even handed the raw rows, the builder does not count an execution as a fact.
    raw = canonical_threads(fabric.entries(ThreadQuery(include_executions=True)))
    assert builder.plan(raw, max(t.seq for t in raw)) == []


def test_overlong_card_text_is_clipped_not_fatal(tmp_path, caplog):
    fabric, wiki, builder, _ = setup(tmp_path)
    at = datetime(2025, 1, 1, tzinfo=UTC)
    fabric.append([draft("a", at, tags=["홍길동"]), draft("b", at, tags=["김철수"])])
    targets = plan(fabric, builder)
    day = next(t for t in targets if t.period == "daily")
    keys = list(day.card_keys)
    assert len(keys) == 1
    response = DigestEntry.model_validate(
        {
            "path": day.path,
            "sentences": [{"text": "요약", "evidence": [SOURCE]}],
            "cards": [
                {
                    "key": keys[0],
                    "headline": " " + "가" * 41,
                    "bullets": ["나" * 90, "b", "c"],
                },
                {"key": keys[0], "headline": "중복", "bullets": []},
            ],
        }
    )
    others = [e for e in entries(targets) if e.path != day.path]
    builder.apply(targets, [response, *others], 1)
    assert (wiki.root / day.path).exists()
    card = card_texts(builder.state_path)["2025-01-01", keys[0]]
    assert card.headline == "가" * 40
    assert card.bullets == ["나" * 80, "b"]


def test_empty_headline_card_is_dropped(tmp_path):
    fabric, wiki, builder, _ = setup(tmp_path)
    fabric.append([draft("a", datetime(2025, 1, 1, tzinfo=UTC))])
    targets = plan(fabric, builder)
    day = next(t for t in targets if t.period == "daily")
    key = next(iter(day.card_keys))
    response = DigestEntry.model_validate(
        {
            "path": day.path,
            "sentences": [],
            "cards": [{"key": key, "headline": " ", "bullets": []}],
        }
    )
    others = [e for e in entries(targets) if e.path != day.path]
    builder.apply(targets, [response, *others], 1)
    assert card_texts(builder.state_path) == {}


def _legacy_future(wiki, builder, path):
    wiki.write(path, {}, "# 옛 일정\n", None, metadata="old", readonly=True)
    with sqlite3.connect(builder.state_path) as db:
        db.execute("INSERT OR REPLACE INTO pages VALUES (?, 'old')", (path,))


def test_stale_future_digest_removed_unless_human_edited(tmp_path):
    fabric, wiki, builder, _ = setup(tmp_path)
    fabric.append([draft("first", datetime(2025, 1, 10, tzinfo=UTC))])
    untouched, edited = "Digest/2025/02/2025-02-03.md", "Digest/2025/02/2025-02-04.md"
    _legacy_future(wiki, builder, untouched)
    _legacy_future(wiki, builder, edited)
    (wiki.root / edited).write_text((wiki.root / edited).read_text() + "\n사람 수정\n")
    plan(fabric, builder)
    assert not (wiki.root / untouched).exists()
    assert (wiki.root / edited).exists()
    with sqlite3.connect(builder.state_path) as db:
        assert (
            db.execute("SELECT 1 FROM pages WHERE path=?", (untouched,)).fetchone()
            is None
        )


def test_refresh_keeps_human_checkbox_toggle(tmp_path):
    fabric, wiki, builder, deferred = setup(tmp_path)
    fabric.append([request("req", datetime(2026, 11, 20, 3, tzinfo=UTC))])
    targets = plan(fabric, builder)
    builder.apply(targets, entries(targets), 1)
    path = "Digest/2026/11/2026-11-20.md"
    file = wiki.root / path
    file.write_text(file.read_text().replace("- [ ] req", "- [x] req"))
    fabric.append(
        [
            draft(
                "done",
                datetime(2026, 11, 21, 3, tzinfo=UTC),
                kind="결정",
                resolves="req",
            )
        ]
    )
    threads = canonical_threads(fabric.entries(ThreadQuery()))
    assert builder.refresh_tasks(threads, max(t.seq for t in threads)) == []
    assert "- [x] req" in file.read_text() and "✅" not in file.read_text()
    assert deferred
