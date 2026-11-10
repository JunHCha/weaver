from datetime import UTC, datetime, timedelta

from weaver.context import ContextDictionary, Page
from weaver.fabric.v2 import FabricStore
from weaver.schema.v2 import ThreadDraft
from weaver.wiki.queries import render_pages, write_pages
from weaver.wiki.store import WikiStore


def add(
    fabric,
    i,
    *,
    kind="기록",
    direction=None,
    field=None,
    value=None,
    tags=None,
    at=None,
    resolves=None,
):
    now = at or datetime(2026, 11, 10, tzinfo=UTC)
    fabric.append(
        [
            ThreadDraft(
                thread_id=str(i),
                kind=kind,
                content=f"내용 {i}",
                occurred_at=now,
                recorded_at=now,
                tags=tags or ["홍길동"],
                field=field,
                value=value,
                source_id="src_00000000000000000000000000",
                actor="human",
                direction=direction,
                resolves=resolves,
                retracts=None,
                correction=None,
            )
        ]
    )


def test_pages_direction_dates_contacts_and_determinism(tmp_path):
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.sync_from_pages([Page("홍길동", "person"), Page("계획", "project")])
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    add(fabric, 1, kind="요청", direction="받음", at=now - timedelta(days=40))
    add(fabric, 2, kind="요청", direction="보냄", at=now - timedelta(days=2))
    add(fabric, 3, field="deadline", value="2026-11-11", at=now - timedelta(days=3))
    add(fabric, 4, field="deadline", value="2026-11-12", at=now - timedelta(days=2))
    add(fabric, 5, field="deadline", value="2026-11-17", at=now - timedelta(days=1))
    add(fabric, 6, tags=["계획"])
    pages = render_pages(fabric, context, now=now, inactive_days=1)
    assert pages == render_pages(fabric, context, now=now, inactive_days=1)
    assert "내용 1" in pages["열린 요청.md"] and "내용 2" in pages["열린 요청.md"]
    assert "받음" in pages["열린 요청.md"] and "보냄" in pages["열린 요청.md"]
    assert "src_00000000000000000000000000" in pages["열린 요청.md"]
    assert (
        "2026-11-17" in pages["다가오는 마감.md"] and "2회" in pages["다가오는 마감.md"]
    )
    assert "홍길동" in pages["사람별 연락.md"] and "계획" not in pages["사람별 연락.md"]


def test_readonly_restore(tmp_path):
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    write_pages(fabric, context, store, now=datetime(2026, 11, 10, tzinfo=UTC))
    page = store.root / "열린 요청.md"
    baseline = page.read_bytes()
    page.write_text("수정")
    store.restore_readonly()
    assert page.read_bytes() == baseline


def test_request_order_resolution_and_contact_boundaries(tmp_path):
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.sync_from_pages([Page("홍길동", "person")])
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    add(fabric, 1, kind="요청", direction="받음", at=now - timedelta(days=31))
    add(fabric, 2, kind="요청", direction="받음", at=now - timedelta(days=30))
    add(fabric, 3, kind="요청", direction="보냄", at=now - timedelta(days=8))
    add(fabric, 4, kind="결정", resolves="3", at=now - timedelta(days=7))
    pages = render_pages(fabric, context, now=now, inactive_days=7)
    assert pages["열린 요청.md"].index("내용 1") < pages["열린 요청.md"].index("내용 2")
    assert "내용 3" not in pages["열린 요청.md"]
    assert "7일 1건, 30일 3건 — 연락 없음" in pages["사람별 연락.md"]


def test_calendar_timezone_and_invalid_values(tmp_path):
    from datetime import timezone

    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    now = datetime(2026, 11, 15, 23, 30, tzinfo=timezone(timedelta(hours=9)))
    add(
        fabric,
        1,
        field="schedule",
        value={"start": "2026-11-15T16:00:00Z", "end": None, "timezone": "UTC"},
        tags=["가"],
    )
    add(fabric, 2, field="deadline", value="invalid", tags=["나"])
    page = render_pages(fabric, context, now=now)["다가오는 마감.md"]
    assert "2026-11-16 가" in page.split("## 다음 주")[1]
    assert "나" not in page
