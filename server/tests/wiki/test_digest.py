from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from weaver.fabric import FabricStore, ThreadQuery
from weaver.schema import ThreadDraft
from weaver.wiki.digest import DigestBuilder, DigestEntry
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
    assert f"[[Events/{SOURCE}]]" in wiki.read("Digest/2025/01/2025-01-10.md")[1]
    assert plan(fabric, builder) == []
    fabric.append([draft("late", datetime(2025, 1, 9, tzinfo=UTC))])
    targets = plan(fabric, builder)
    assert len(targets) == 3
    builder.apply(targets, entries(targets), 2)
    assert not (wiki.root / "Digest/2025/01/2025-01-08.md").exists()


def test_schedule_includes_future_day_week_month_and_superseded_value(tmp_path):
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
    assert {
        "Digest/2025/02/2025-02-03.md",
        "Digest/Weekly/2025-W06.md",
        "Digest/Monthly/2025-02.md",
    } <= paths
    builder.apply(targets, entries(targets), 1)
    assert "old" in wiki.read("Digest/2025/02/2025-02-03.md")[1]
    fabric.append(
        [
            draft(
                "new",
                at.replace(day=2),
                field="schedule",
                value={"start": "2025-02-04T00:00:00Z", "end": None, "timezone": "UTC"},
            )
        ]
    )
    targets = plan(fabric, builder)
    builder.apply(targets, entries(targets), 2)
    assert "old" not in wiki.read("Digest/2025/02/2025-02-03.md")[1]
    assert "new" in wiki.read("Digest/2025/02/2025-02-04.md")[1]


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
