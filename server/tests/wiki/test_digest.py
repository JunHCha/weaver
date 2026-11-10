from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from weaver.fabric.v2 import FabricStore
from weaver.schema.v2 import ThreadDraft
from weaver.wiki.digest import DigestBuilder
from weaver.wiki.store import HumanEditPending, WikiStore

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


class FakeLlm:
    def __init__(self):
        self.calls = []

    def generate_json(self, task, system, user, schema):
        import json

        data = json.loads(user)
        self.calls.append((task, data))
        return schema.model_validate(
            {"sentences": [{"text": "요약", "evidence": [data["evidence"][0]]}]}
        )


def setup(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    fabric = FabricStore(tmp_path / "fabric.db")
    wiki = WikiStore(vault, tmp_path / "wiki-state", settle_seconds=0)
    llm = FakeLlm()
    builder = DigestBuilder(
        fabric,
        wiki,
        llm,
        tmp_path / "digest.db",
        ZoneInfo("Asia/Seoul"),
        lambda source: f"Events/{source}",
    )
    return fabric, wiki, llm, builder


def test_daily_late_arrival_regenerates_week_and_month_and_skips_empty(tmp_path):
    fabric, wiki, llm, builder = setup(tmp_path)
    fabric.append([draft("first", datetime(2025, 1, 10, tzinfo=UTC))])
    builder.update()
    assert len(llm.calls) == 3
    assert f"[[Events/{SOURCE}]]" in wiki.read("Digest/2025/01/2025-01-10.md")[1]
    builder.update()
    assert len(llm.calls) == 3
    fabric.append([draft("late", datetime(2025, 1, 9, tzinfo=UTC))])
    builder.update()
    assert len(llm.calls) == 6
    assert not (wiki.root / "Digest/2025/01/2025-01-08.md").exists()


def test_local_day_and_deterministic_current_fields_and_requests(tmp_path):
    fabric, wiki, _, builder = setup(tmp_path)
    at = datetime(2025, 1, 1, 16, tzinfo=UTC)
    fabric.append(
        [
            draft("due", at, field="deadline", value="2025-01-02"),
            draft("request", at, kind="요청", direction="받음"),
        ]
    )
    builder.update()
    body = wiki.read("Digest/2025/01/2025-01-02.md")[1]
    assert body.index("마감") < body.index("요약")
    assert "due" in body and "request" in body
    assert not (wiki.root / "Digest/2025/01/2025-01-01.md").exists()


def test_human_edits_and_failed_write_retry(tmp_path):
    fabric, wiki, llm, builder = setup(tmp_path)
    at = datetime(2025, 1, 1, tzinfo=UTC)
    fabric.append([draft("first", at)])
    builder.update()
    path = "Digest/2025/01/2025-01-01.md"
    target = wiki.root / path
    target.write_text(target.read_text() + "\n사람 수정\n")
    fabric.append([draft("next", at)])
    wiki.settle_seconds = 999999
    with pytest.raises(HumanEditPending):
        builder.update()
    wiki.settle_seconds = 0
    builder.update()
    assert any("사람 수정" in call[1]["previous"] for call in llm.calls)


def test_schedule_updates_future_day_and_superseded_value_is_removed(tmp_path):
    fabric, wiki, _, builder = setup(tmp_path)
    at = datetime(2025, 1, 1, tzinfo=UTC)
    fabric.append(
        [
            draft(
                "old",
                at,
                field="schedule",
                value={"start": "2025-01-03T00:00:00Z", "end": None, "timezone": "UTC"},
            )
        ]
    )
    builder.update()
    assert "old" in wiki.read("Digest/2025/01/2025-01-03.md")[1]
    fabric.append(
        [
            draft(
                "new",
                at.replace(day=2),
                field="schedule",
                value={"start": "2025-01-04T00:00:00Z", "end": None, "timezone": "UTC"},
            )
        ]
    )
    builder.update()
    assert "old" not in wiki.read("Digest/2025/01/2025-01-03.md")[1]
    assert "new" in wiki.read("Digest/2025/01/2025-01-04.md")[1]


def test_invalid_evidence_does_not_advance_bookmark(tmp_path):
    fabric, wiki, llm, builder = setup(tmp_path)
    fabric.append([draft("first", datetime(2025, 1, 1, tzinfo=UTC))])
    original = llm.generate_json

    def invalid(task, system, user, schema):
        return schema.model_validate(
            {"sentences": [{"text": "주장", "evidence": ["unknown"]}]}
        )

    llm.generate_json = invalid
    with pytest.raises(ValueError, match="evidence"):
        builder.update()
    llm.generate_json = original
    builder.update()
    assert wiki.read("Digest/2025/01/2025-01-01.md")[1]
