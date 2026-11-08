"""#9 alert policy table: synthetic Fabric facts and pending proposals only."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from weaver.alert import AlertHistory, ScheduleAlertDetector, alert_id_for
from weaver.fabric import EntryQuery, FabricStore
from weaver.schema import Entry, Proposal

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=SEOUL)
MEETING = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA1"
PERSON = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA2"
PRP = "prp_01ARZ3NDEKTSV4RRFFQ69G5FA0"
SRC = "src_01ARZ3NDEKTSV4RRFFQ69G5FA0"
SECRET = "비밀 원문 점검 회의"


def fact(n: int, field: str | None, value: object, entity: str = MEETING) -> Entry:
    return Entry.model_validate(
        dict(
            schema_version=1,
            entry_id=f"evt_{n:026d}",
            entities=[entity],
            kind="일정",
            field=field,
            value=value,
            occurred_at=datetime(2026, 11, 1, tzinfo=UTC),
            recorded_at=datetime(2026, 11, 1, n, tzinfo=UTC),
            source_id=f"src_{n:026d}",
            actor="ai_approved",
            supersedes=None,
            retracts=None,
            request=None,
            resolves=None,
            blocked_by_request=None,
        )
    )


def evidence() -> dict[str, object]:
    return {"confidence": 0.9, "ask": None, "thread_excerpt": SECRET}


def draft(ref: str, field: str | None, value: object, kind: str = "일정") -> dict:
    return evidence() | {
        "ref": ref,
        "entities": ["e1"],
        "kind": kind,
        "field": field,
        "value": value,
        "occurred_at": "2026-11-08T10:00:00+09:00",
        "supersedes": None,
        "retracts": None,
        "request": None,
        "resolves": None,
        "blocked_by_request": None,
    }


def schedule(ref: str, entry_ref: str, start: str = "2026-11-12T15:00:00+09:00"):
    return evidence() | {
        "ref": ref,
        "title": SECRET,
        "start": start,
        "end": None,
        "entities": ["e1"],
        "entry_ref": entry_ref,
        "blocked_by_request": None,
    }


def proposal(
    drafts: list[dict],
    schedules: list[dict] | None = None,
    *,
    rev: int = 1,
    entity_id: str | None = MEETING,
    entity_type: str = "meeting",
) -> Proposal:
    return Proposal.model_validate(
        {
            "schema_version": 1,
            "proposal_id": PRP,
            "rev": rev,
            "source": {
                "source_id": SRC,
                "channel": "kakao",
                "captured_at": "2026-11-08T10:00:00+09:00",
                "occurred_at": None,
            },
            "summary": SECRET,
            "thread_excerpt": SECRET,
            "entities": [
                evidence()
                | {
                    "ref": "e1",
                    "entity_id": entity_id,
                    "type": entity_type,
                    "name": SECRET,
                    "aliases": [],
                }
            ],
            "schedules": schedules
            if schedules is not None
            else [schedule("s1", drafts[0]["ref"])],
            "relations": [],
            "entry_drafts": drafts,
        }
    )


def moved(new: object = "2026-11-12T15:00:00+09:00", **kw) -> Proposal:
    return proposal([draft("d1", "date", new)], **kw)


@pytest.fixture
def fabric(tmp_path: Path) -> FabricStore:
    return FabricStore(tmp_path / "fabric.sqlite3")


def detector(tmp_path: Path, fabric: FabricStore, **kw) -> ScheduleAlertDetector:
    return ScheduleAlertDetector(
        fabric,
        AlertHistory(tmp_path / "alerts.sqlite3"),
        timezone=SEOUL,
        clock=lambda: NOW,
        **kw,
    )


def test_future_time_change_alerts_without_touching_fabric(tmp_path, fabric):
    fabric.append([fact(1, "date", "2026-11-10T15:00:00+09:00")])
    before = fabric.entries(EntryQuery())
    [alert] = detector(tmp_path, fabric).detect(moved())
    assert alert.proposal_id == PRP
    assert alert.schedule_ref == "s1"
    assert alert.created_at == NOW
    assert alert.message == "11월 10일 일정의 시각이 바뀔 수 있음. 승인 전 정보입니다."
    assert SECRET not in alert.model_dump_json()
    assert fabric.entries(EntryQuery()) == before


@pytest.mark.parametrize(
    "new",
    [
        "2026-11-10T15:00:00+09:00",  # same value (already reflected)
        "2026-11-10T06:00:00Z",  # same instant, other offset
    ],
)
def test_same_value_does_not_alert(tmp_path, fabric, new):
    fabric.append([fact(1, "date", "2026-11-10T15:00:00+09:00")])
    assert detector(tmp_path, fabric).detect(moved(new)) == []


@pytest.mark.parametrize(
    ("existing", "alerts"),
    [
        ("2026-11-08T11:59:59+09:00", 0),  # past schedule
        ("2026-11-08T12:00:00+09:00", 1),  # window start is inclusive
        ("2026-12-08T11:59:59+09:00", 1),  # just inside the 30-day horizon
        ("2026-12-08T12:00:00+09:00", 0),  # window end is exclusive
        ("2026-11-08", 1),  # all-day today is still ahead until midnight
        ("2026-11-07", 0),  # all-day yesterday is past
    ],
)
def test_comparison_window(tmp_path, fabric, existing, alerts):
    fabric.append([fact(1, "date", existing)])
    found = detector(tmp_path, fabric).detect(moved("2026-12-20T09:00:00+09:00"))
    assert len(found) == alerts


def test_far_schedule_moved_into_window_alerts(tmp_path, fabric):
    fabric.append([fact(1, "date", "2027-02-01T10:00:00+09:00")])
    assert len(detector(tmp_path, fabric).detect(moved())) == 1


def test_all_day_and_timed_values_differ(tmp_path, fabric):
    fabric.append([fact(1, "date", "2026-11-12")])
    assert len(detector(tmp_path, fabric).detect(moved())) == 1
    fabric.append([fact(2, "date", "2026-11-12T15:00:00+09:00")])
    assert detector(tmp_path, fabric).detect(moved()) == []


def test_unrelated_targets_are_ignored(tmp_path, fabric):
    fabric.append([fact(1, "date", "2026-11-10T15:00:00+09:00")])
    # A new (unmatched) entity has no approved schedule to change.
    assert detector(tmp_path, fabric).detect(moved(entity_id=None)) == []
    # Another existing entity without a schedule in Fabric.
    assert detector(tmp_path, fabric).detect(moved(entity_id=PERSON)) == []


@pytest.mark.parametrize("status", ["취소", "완료", "cancelled", "done"])
def test_cancelled_or_done_schedules_are_ignored(tmp_path, fabric, status):
    fabric.append(
        [
            fact(1, "date", "2026-11-10T15:00:00+09:00"),
            fact(2, "status", status),
        ]
    )
    assert detector(tmp_path, fabric).detect(moved()) == []


def test_assignee_and_status_changes_on_future_schedule(tmp_path, fabric):
    fabric.append(
        [
            fact(1, "date", "2026-11-10T15:00:00+09:00"),
            fact(2, "assignee", "김"),
            fact(3, "status", "예정"),
            fact(4, "assignees", {"add": ["a"], "remove": []}),
        ]
    )
    drafts = [
        draft("d1", None, "일정"),
        draft("d2", "assignee", "이", kind="변경"),
        draft("d3", "status", "보류", kind="변경"),
    ]
    kept = [schedule("s1", "d1", start="2026-11-10T15:00:00+09:00")]
    [alert] = detector(tmp_path, fabric).detect(proposal(drafts, kept))
    assert alert.message == (
        "11월 10일 일정의 담당자·상태가 바뀔 수 있음. 승인 전 정보입니다."
    )
    # An assignees operation that changes nothing is not a change.
    same = [
        draft("d1", None, "일정"),
        draft("d2", "assignees", {"add": ["a"], "remove": ["z"]}, kind="변경"),
    ]
    assert detector(tmp_path, fabric).detect(proposal(same, kept)) == []
    added = [
        draft("d1", None, "일정"),
        draft("d2", "assignees", {"add": ["b"], "remove": []}, kind="변경"),
    ]
    [alert] = detector(tmp_path, fabric).detect(proposal(added, kept))
    assert "담당자가" in alert.message


def test_deadline_change_label(tmp_path, fabric):
    fabric.append([fact(1, "deadline", "2026-11-20")])
    [alert] = detector(tmp_path, fabric).detect(
        proposal(
            [draft("d1", None, "일정"), draft("d2", "deadline", "2026-11-25", "변경")]
        )
    )
    assert alert.message == "11월 20일 일정의 마감이 바뀔 수 있음. 승인 전 정보입니다."


def test_meeting_schedule_start_is_compared_with_fabric_date(tmp_path, fabric):
    fabric.append([fact(1, "date", "2026-11-10T15:00:00+09:00")])
    pending = proposal([draft("d1", None, "일정")])  # start 11-12 only in Schedule
    assert len(detector(tmp_path, fabric).detect(pending)) == 1
    # Not for a project: its schedules are separate events, not its own time.
    other = proposal([draft("d1", None, "일정")], entity_type="project")
    assert detector(tmp_path, fabric).detect(other) == []
    same = proposal(
        [draft("d1", None, "일정")],
        [schedule("s1", "d1", start="2026-11-10T15:00:00+09:00")],
    )
    assert detector(tmp_path, fabric).detect(same) == []


def test_no_repeat_across_restart_and_revisions(tmp_path, fabric):
    fabric.append([fact(1, "date", "2026-11-10T15:00:00+09:00")])
    first = detector(tmp_path, fabric)
    [alert] = first.detect(moved())
    # Not yet published (e.g. crash): retry returns the same deterministic ID.
    assert [a.alert_id for a in detector(tmp_path, fabric).detect(moved())] == [
        alert.alert_id
    ]
    first.published(alert)
    restarted = detector(tmp_path, fabric)
    assert restarted.detect(moved()) == []  # even if the phone deleted the file
    assert restarted.detect(moved(rev=2)) == []  # same change in a revised rev
    [revised] = restarted.detect(moved("2026-11-13T15:00:00+09:00", rev=2))
    assert revised.alert_id != alert.alert_id
    assert AlertHistory(tmp_path / "alerts.sqlite3").basis(alert.alert_id) == [
        "evt_00000000000000000000000001"
    ]


def test_approved_change_is_not_alerted_again(tmp_path, fabric):
    fabric.append([fact(1, "date", "2026-11-10T15:00:00+09:00")])
    fabric.append([fact(2, "date", "2026-11-12T15:00:00+09:00")])  # approval
    assert detector(tmp_path, fabric).detect(moved()) == []


def test_alert_count_is_limited_per_proposal(tmp_path, fabric):
    entities = [f"ent_01ARZ3NDEKTSV4RRFFQ69G5FB{i}" for i in range(5)]
    fabric.append(
        [
            fact(i + 1, "date", f"2026-11-{10 + i}T15:00:00+09:00", entity)
            for i, entity in enumerate(entities)
        ]
    )

    def many(rev: int = 1, count: int = 5) -> Proposal:
        payload = moved().model_dump(mode="json", by_alias=True)
        payload["rev"] = rev
        payload["entities"] = [
            evidence()
            | {
                "ref": f"e{i + 1}",
                "entity_id": entity,
                "type": "meeting",
                "name": "x",
                "aliases": [],
            }
            for i, entity in enumerate(entities[:count])
        ]
        payload["entry_drafts"] = [
            draft(f"d{i + 1}", "date", "2026-11-30T09:00:00+09:00")
            | {"entities": [f"e{i + 1}"]}
            for i in range(count)
        ]
        payload["schedules"] = [
            schedule(f"s{i + 1}", f"d{i + 1}") | {"entities": [f"e{i + 1}"]}
            for i in range(count)
        ]
        return Proposal.model_validate(payload)

    run = detector(tmp_path, fabric, max_alerts=3)
    found = run.detect(many())
    # Earliest affected schedules first.
    assert [a.schedule_ref for a in found] == ["s1", "s2", "s3"]
    for alert in found:
        run.published(alert)
    # The lifetime limit holds for later revisions and restarts.
    assert detector(tmp_path, fabric, max_alerts=3).detect(many(rev=2)) == []


def test_alert_id_is_deterministic_and_schema_shaped():
    key = alert_id_for(PRP, "s1", "sig")
    assert key == alert_id_for(PRP, "s1", "sig")
    assert key != alert_id_for(PRP, "s2", "sig")
    assert key.startswith("alt_") and len(key) == 30 and key[4] in "01234567"


def test_unparseable_values_fall_back_to_json_equality(tmp_path, fabric):
    fabric.append([fact(1, "date", "2026-11-10T15:00:00+09:00")])
    # A garbled new time is still a contradiction of the approved value.
    assert len(detector(tmp_path, fabric).detect(moved("다음 주"))) == 1


def test_local_date_is_used_for_message(tmp_path, fabric):
    fabric.append([fact(1, "date", "2026-11-30T16:00:00Z")])  # 12-01 in Seoul
    [alert] = detector(tmp_path, fabric).detect(moved())
    assert alert.message.startswith("12월 1일 일정")
