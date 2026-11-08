"""Resident loop lifecycle with fake workers: no Syncthing, LLM or launchd."""

import json
import logging
import os
import signal
import stat
import threading
from dataclasses import dataclass, field

import pytest

from weaver.runtime import (
    AlreadyRunning,
    InstanceLock,
    LlmStatus,
    MaintenanceTask,
    Outcome,
    Runtime,
    RuntimeLedger,
)


@dataclass(frozen=True)
class FakeSource:
    source_id: str
    channel: str = "kakao"


@dataclass
class FakeScanner:
    scans: int = 0
    on_scan: list = field(default_factory=list)

    def scan_once(self) -> None:
        self.scans += 1
        for hook in self.on_scan:
            hook(self.scans)


@dataclass
class FakeSources:
    items: list = field(default_factory=list)

    def sources(self):
        return list(self.items)


@dataclass
class FakeProcessor:
    outcomes: dict = field(default_factory=dict)
    seen: list = field(default_factory=list)

    def process(self, source) -> Outcome:
        self.seen.append(source.source_id)
        result = self.outcomes.get(source.source_id, "done")
        if isinstance(result, BaseException):
            raise result
        return result


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class Waiter:
    """Replaces Event.wait: advances the fake clock and stops after N waits."""

    def __init__(self, clock: Clock, stop: threading.Event, waits: int) -> None:
        self.clock, self.stop, self.left = clock, stop, waits
        self.gaps: list[float] = []
        self.next_gap: float | None = None

    def __call__(self, timeout: float) -> bool:
        self.gaps.append(timeout)
        self.clock.now += self.next_gap if self.next_gap is not None else timeout
        self.next_gap = None
        self.left -= 1
        if self.left <= 0:
            self.stop.set()
        return self.stop.is_set()


def make(tmp_path, *, scanner=None, sources=None, processor=None, waits=1, **kw):
    clock = Clock()
    stop = threading.Event()
    waiter = Waiter(clock, stop, waits)
    runtime = Runtime(
        state_root=tmp_path,
        scanner=scanner or FakeScanner(),
        sources=sources or FakeSources(),
        processor=processor or FakeProcessor(),
        ledger=RuntimeLedger(tmp_path / "runtime.sqlite3"),
        llm=kw.pop("llm", LlmStatus(True, "configured")),
        interval=5.0,
        clock=clock,
        wait=waiter,
        **kw,
    )
    return runtime, stop, waiter, clock


def test_startup_rescans_backlog_and_never_reprocesses_done(tmp_path):
    sources = FakeSources([FakeSource("src_a"), FakeSource("src_b")])
    processor = FakeProcessor()
    scanner = FakeScanner()
    runtime, stop, _, _ = make(
        tmp_path, scanner=scanner, sources=sources, processor=processor, waits=2
    )
    runtime.run(stop)
    # Startup scan handles files that arrived while the process was down,
    # then every interval rescans; completed sources are not handed out twice.
    assert scanner.scans == 2
    assert processor.seen == ["src_a", "src_b"]

    restarted = FakeProcessor()
    again, stop2, _, _ = make(tmp_path, sources=sources, processor=restarted)
    again.run(stop2)
    assert restarted.seen == []
    assert RuntimeLedger(tmp_path / "runtime.sqlite3").counts() == {"done": 2}


def test_resume_after_sleep_gap_is_detected_and_rescanned(tmp_path, caplog):
    sources = FakeSources()
    processor = FakeProcessor()
    scanner = FakeScanner()
    runtime, stop, waiter, _ = make(
        tmp_path, scanner=scanner, sources=sources, processor=processor, waits=3
    )

    def arrive_while_asleep(n: int) -> None:
        if n == 2:
            waiter.next_gap = 3600.0  # the machine sleeps after this tick
        if n == 3:
            # Files synced while asleep are found by the first post-wake rescan.
            sources.items.append(FakeSource("src_sleep"))

    scanner.on_scan.append(arrive_while_asleep)
    with caplog.at_level(logging.INFO, logger="weaver.runtime"):
        runtime.run(stop)
    assert processor.seen == ["src_sleep"]
    assert runtime.resumes == 1
    assert any("resume detected" in r.getMessage() for r in caplog.records)
    status = json.loads((tmp_path / "runtime-status.json").read_text())
    assert status["resumes"] == 1
    assert status["sources"] == {"done": 1}


def test_sigterm_finishes_current_tick_then_exits(tmp_path):
    sources = FakeSources([FakeSource("src_a"), FakeSource("src_b")])
    scanner = FakeScanner()

    class Terminating(FakeProcessor):
        def process(self, source):
            if not self.seen:
                os.kill(os.getpid(), signal.SIGTERM)
            return super().process(source)

    processor = Terminating()
    clock = Clock()
    runtime = Runtime(
        state_root=tmp_path,
        scanner=scanner,
        sources=sources,
        processor=processor,
        ledger=RuntimeLedger(tmp_path / "runtime.sqlite3"),
        llm=LlmStatus(True, "configured"),
        interval=60.0,
        clock=clock,
    )
    previous = signal.getsignal(signal.SIGTERM)
    code = runtime.serve()
    assert code == 0
    # The in-flight source completed durably; the next one was not started.
    assert processor.seen == ["src_a"]
    assert scanner.scans == 1
    assert RuntimeLedger(tmp_path / "runtime.sqlite3").counts() == {"done": 1}
    assert signal.getsignal(signal.SIGTERM) == previous
    assert json.loads((tmp_path / "runtime-status.json").read_text())["state"] == (
        "stopped"
    )
    # The instance lock was released on exit.
    with InstanceLock(tmp_path / "weaver.lock"):
        pass


def test_duplicate_instance_refuses_to_run(tmp_path):
    processor = FakeProcessor()
    runtime, _, _, _ = make(
        tmp_path, sources=FakeSources([FakeSource("src_a")]), processor=processor
    )
    with InstanceLock(tmp_path / "weaver.lock"):
        with pytest.raises(AlreadyRunning):
            with InstanceLock(tmp_path / "weaver.lock"):
                pass
        assert runtime.serve() == 3
    assert processor.seen == []
    mode = stat.S_IMODE((tmp_path / "weaver.lock").stat().st_mode)
    assert mode == 0o600


def test_failures_are_classified_and_backed_off(tmp_path, caplog):
    secret = "원문 비밀 내용"
    processor = FakeProcessor(
        {
            "src_bad": ValueError(secret),
            "src_flaky": OSError(secret),
            "src_llm": "waiting_llm",
        }
    )
    sources = FakeSources(
        [FakeSource("src_bad"), FakeSource("src_flaky"), FakeSource("src_llm")]
    )
    runtime, stop, _, _ = make(
        tmp_path,
        sources=sources,
        processor=processor,
        waits=3,
        llm=LlmStatus(False, "api_key_missing"),
    )
    with caplog.at_level(logging.DEBUG):
        runtime.run(stop)
    # Ticks at t=0,5,10. Permanent: once. Waiting for LLM: not retried while
    # unavailable. Transient: retried only after its 10s backoff (t=10).
    assert processor.seen.count("src_bad") == 1
    assert processor.seen.count("src_llm") == 1
    assert processor.seen.count("src_flaky") == 2
    counts = RuntimeLedger(tmp_path / "runtime.sqlite3").counts()
    assert counts == {"failed": 1, "retry": 1, "waiting_llm": 1}
    assert all(secret not in r.getMessage() for r in caplog.records)
    assert any(
        "llm stage skipped: api_key_missing" in r.getMessage() for r in caplog.records
    )
    status = json.loads((tmp_path / "runtime-status.json").read_text())
    assert status["llm"] == {"available": False, "reason": "api_key_missing"}


def test_waiting_llm_sources_resume_when_llm_available(tmp_path):
    sources = FakeSources([FakeSource("src_llm")])
    first = FakeProcessor({"src_llm": "waiting_llm"})
    runtime, stop, _, _ = make(
        tmp_path, sources=sources, processor=first, llm=LlmStatus(False, "none")
    )
    runtime.run(stop)
    second = FakeProcessor()
    again, stop2, _, _ = make(tmp_path, sources=sources, processor=second)
    again.run(stop2)
    assert second.seen == ["src_llm"]


def test_tick_failure_backs_off_without_crashing(tmp_path):
    class Broken:
        def scan_once(self):
            raise RuntimeError("boom with 원문")

    runtime, stop, waiter, _ = make(tmp_path, scanner=Broken(), waits=4)
    runtime.run(stop)
    assert waiter.gaps == [10.0, 20.0, 40.0, 80.0]


def test_maintenance_runs_on_its_own_interval(tmp_path):
    calls = []
    task = MaintenanceTask("purge", 12.0, lambda: calls.append(1))
    runtime, stop, _, _ = make(tmp_path, maintenance=[task], waits=5)
    runtime.run(stop)
    # Ticks at t=0,5,10,15,20: due at 0 and 15 (>= 12s since the last run).
    assert len(calls) == 2


def test_ledger_and_status_files_are_private(tmp_path):
    runtime, stop, _, _ = make(tmp_path, sources=FakeSources([FakeSource("src_a")]))
    runtime.run(stop)
    for name in ("runtime.sqlite3", "runtime-status.json"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    ledger = RuntimeLedger(tmp_path / "runtime.sqlite3")
    assert ledger.pragma("journal_mode") == "wal"
    assert ledger.pragma("synchronous") == 2  # FULL
