"""Change alerts compare newly approved schedules with earlier approved ones."""

import os
import re
import stat
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from weaver.alert import AlertHistory, ScheduleAlertDetector, alert_id_for
from weaver.schema import Alert, Thread

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=SEOUL)
SRC = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
PKG = "pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0"
SECRET = "비밀 프로젝트 회의"


def at(day, hour=15, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=SEOUL)


def schedule(
    seq,
    start,
    *,
    content=SECRET,
    end=None,
    tags=("홍길동",),
    zone="Asia/Seoul",
    kind="일정",
    value=None,
    retracts=None,
):
    return Thread.model_validate(
        {
            "seq": seq,
            "thread_id": f"thr_{seq}",
            "kind": kind,
            "content": content,
            "occurred_at": NOW.isoformat(),
            "recorded_at": NOW.isoformat(),
            "tags": list(tags),
            "field": "schedule",
            "value": value
            if value is not None
            else {
                "start": start.isoformat(),
                "end": None if end is None else end.isoformat(),
                "timezone": zone,
            },
            "source_id": SRC,
            "actor": "ai_approved",
            "direction": None,
            "resolves": None,
            "retracts": retracts,
            "correction": None,
        }
    )


@pytest.fixture
def detector(tmp_path):
    return ScheduleAlertDetector(
        AlertHistory(tmp_path / "alerts.sqlite3"), timezone=SEOUL, clock=lambda: NOW
    )


def detect(detector, new, *earlier, package=PKG):
    return detector.detect(package, [new], [*earlier, new])


def test_changed_start_alerts_without_leaking_content(detector):
    old, new = schedule(1, at(10)), schedule(2, at(10, 17))
    [alert] = detect(detector, new, old)
    assert isinstance(alert, Alert) and alert.schema_version == 2
    assert (alert.package_id, alert.thread_id) == (PKG, "thr_2")
    assert alert.message.startswith("11월 10일 일정의 시각이 바뀌었을 수 있음")
    assert SECRET not in alert.message and "홍길동" not in alert.message
    assert re.fullmatch(r"alt_[0-7][0-9A-HJKMNP-TV-Z]{25}", alert.alert_id)


def test_changed_end_alerts(detector):
    old = schedule(1, at(10), end=at(10, 16))
    assert detect(detector, schedule(2, at(10), end=at(10, 17)), old)


@pytest.mark.parametrize(
    "new",
    [
        schedule(2, at(10)),
        schedule(2, at(10), zone="UTC"),
        schedule(2, at(10), content=f" {SECRET.upper()}  "),
    ],
    ids=["same", "timezone_name_only", "content_folded"],
)
def test_same_instants_are_not_a_change(detector, new):
    assert detect(detector, new, schedule(1, at(10))) == []


@pytest.mark.parametrize(
    ("earlier", "expected"),
    [
        (schedule(1, at(10), content="다른 회의"), 0),
        (schedule(1, at(10), tags=("김철수",)), 0),
        (schedule(1, at(10), tags=()), 1),
        (schedule(1, at(10), kind="기록"), 0),
        (schedule(1, at(6, 9)), 0),  # already past
        (schedule(1, at(10), tags=("김철수", "홍길동")), 1),
    ],
    ids=["content", "tag", "untagged", "not_schedule", "past", "overlap"],
)
def test_event_matching_and_window(detector, earlier, expected):
    assert len(detect(detector, schedule(2, at(10, 17)), earlier)) == expected


def test_window_is_checked_against_old_or_new_time(detector):
    far = NOW + timedelta(days=90)
    far_old = schedule(1, far)
    assert detect(detector, schedule(2, far + timedelta(hours=1)), far_old) == []
    assert detect(detector, schedule(3, at(10)), far_old)  # moved into the window


def test_retractions_are_ignored(detector):
    old = schedule(1, at(10))
    retraction = schedule(
        3,
        at(10),
        value={"start": at(10).isoformat(), "end": None, "timezone": "Asia/Seoul"},
        retracts="thr_1",
    )
    new = schedule(2, at(10, 17))
    assert detector.detect(PKG, [new], [old, new, retraction]) == []
    retracted_new = schedule(4, at(10, 18), retracts="thr_2")
    assert detector.detect(PKG, [new], [old, new, retracted_new]) == []


def test_latest_earlier_schedule_is_the_approved_one(detector):
    first, second = schedule(1, at(10)), schedule(2, at(10, 17))
    assert detect(detector, schedule(3, at(10, 17)), first, second) == []
    assert detect(detector, schedule(3, at(10, 18)), first, second)


def test_only_earlier_threads_outside_the_package_count(detector):
    one, two = schedule(1, at(10)), schedule(2, at(10, 17))
    assert detector.detect(PKG, [one, two], [one, two]) == []
    assert detector.detect(PKG, [one], [one, two]) == []


def test_published_alerts_are_not_repeated_across_restart(tmp_path):
    def build():
        return ScheduleAlertDetector(
            AlertHistory(tmp_path / "alerts.sqlite3"),
            timezone=SEOUL,
            clock=lambda: NOW,
        )

    old, new = schedule(1, at(10)), schedule(2, at(10, 17))
    first = build()
    [alert] = detect(first, new, old)
    # Not yet durably written: the next pass offers it again.
    assert detect(first, new, old) == [alert]
    first.published(alert)
    assert detect(first, new, old) == []
    assert detect(build(), new, old) == []
    assert AlertHistory(tmp_path / "alerts.sqlite3").basis(alert.alert_id) == "thr_1"


def test_changed_value_is_a_new_alert(detector):
    old = schedule(1, at(10))
    [first] = detect(detector, schedule(2, at(10, 17)), old)
    detector.published(first)
    [second] = detect(detector, schedule(2, at(10, 18)), old)
    assert second.alert_id != first.alert_id


def test_alert_count_is_limited_per_package(detector):
    pairs = [
        (schedule(2 * i + 1, at(10 + i)), schedule(2 * i + 2, at(10 + i, 18)))
        for i in range(5)
    ]
    snapshot = [t for pair in pairs for t in pair]
    selected = [new for _, new in pairs]
    alerts = detector.detect(PKG, selected, snapshot)
    assert len(alerts) == 3
    assert [a.message[:7] for a in alerts] == ["11월 10일", "11월 11일", "11월 12일"]
    for alert in alerts:
        detector.published(alert)
    assert detector.detect(PKG, selected, snapshot) == []
    assert len(detector.detect("pkg_other", selected, snapshot)) == 3


def test_alert_id_is_deterministic_and_schema_shaped():
    one = alert_id_for(PKG, "thr_2", "sig")
    assert one == alert_id_for(PKG, "thr_2", "sig")
    assert one != alert_id_for(PKG, "thr_2", "other")
    assert re.fullmatch(r"alt_[0-7][0-9A-HJKMNP-TV-Z]{25}", one)


def test_history_is_private_wal(tmp_path):
    path = tmp_path / "state" / "alerts.sqlite3"
    AlertHistory(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
