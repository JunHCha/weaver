"""Resident loop lifecycle with fake workers: no Syncthing, LLM or launchd."""

import json
import logging
import os
import signal
import stat
import threading
from dataclasses import asdict, dataclass, field

import pytest

from weaver.llm.errors import (
    AuthenticationError,
    BudgetExceeded,
    InvalidJson,
    LlmTimeout,
    RateLimited,
    Refusal,
    SchemaMismatch,
    TransientError,
)
from weaver.runtime import (
    AlreadyRunning,
    InstanceLock,
    Interrupted,
    LlmGate,
    LlmStatus,
    MaintenanceTask,
    Runtime,
    RuntimeLedger,
)
from weaver.runtime.gate import ExtractionInvalid
from weaver.runtime.loop import LLM_RECHECK_SECONDS


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
    raw: dict = field(default_factory=dict)  # source_id -> damaged JSON
    excluded: list = field(default_factory=list)

    def source_rows(self, exclude=()):
        self.excluded.append(set(exclude))
        rows = [(s.source_id, json.dumps(asdict(s))) for s in self.items]
        rows += list(self.raw.items())
        return [row for row in rows if row[0] not in exclude]


def decode(raw: str) -> FakeSource:
    return FakeSource(**json.loads(raw))


@dataclass
class FakeProcessor:
    outcomes: dict = field(default_factory=dict)
    seen: list = field(default_factory=list)

    def process(self, source, stop):
        self.seen.append(source.source_id)
        result = self.outcomes.get(source.source_id, "done")
        if isinstance(result, list):
            result = result.pop(0) if len(result) > 1 else result[0]
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


def gate(available=True, reason="configured"):
    return LlmGate(lambda: LlmStatus(available, reason))


def make(tmp_path, *, scanner=None, sources=None, processor=None, waits=1, **kw):
    clock = Clock()
    stop = threading.Event()
    waiter = Waiter(clock, stop, waits)
    runtime = Runtime(
        state_root=tmp_path,
        scanner=scanner or FakeScanner(),
        sources=sources or FakeSources(),
        decode=decode,
        processor=processor or FakeProcessor(),
        ledger=RuntimeLedger(tmp_path / "runtime.sqlite3"),
        gate=kw.pop("gate", None) or gate(),
        interval=5.0,
        clock=clock,
        wait=waiter,
        **kw,
    )
    return runtime, stop, waiter, clock


def counts(tmp_path):
    return RuntimeLedger(tmp_path / "runtime.sqlite3").counts()


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
    # done IDs are excluded in the archive query, not after reading rows.
    assert sources.excluded[-1] == {"src_a", "src_b"}

    restarted = FakeProcessor()
    again, stop2, _, _ = make(tmp_path, sources=sources, processor=restarted)
    again.run(stop2)
    assert restarted.seen == []
    assert counts(tmp_path) == {"done": 2}


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


def test_sigterm_stops_between_sources_then_exits(tmp_path):
    sources = FakeSources([FakeSource("src_a"), FakeSource("src_b")])
    scanner = FakeScanner()

    class Terminating(FakeProcessor):
        def process(self, source, stop):
            if not self.seen:
                os.kill(os.getpid(), signal.SIGTERM)
            return super().process(source, stop)

    processor = Terminating()
    runtime = Runtime(
        state_root=tmp_path,
        scanner=scanner,
        sources=sources,
        decode=decode,
        processor=processor,
        ledger=RuntimeLedger(tmp_path / "runtime.sqlite3"),
        gate=gate(),
        interval=60.0,
        clock=Clock(),
    )
    previous = signal.getsignal(signal.SIGTERM)
    code = runtime.serve()
    assert code == 0
    # The in-flight source completed durably; the next one was not started.
    assert processor.seen == ["src_a"]
    assert scanner.scans == 1
    assert counts(tmp_path) == {"done": 1}
    assert signal.getsignal(signal.SIGTERM) == previous
    assert json.loads((tmp_path / "runtime-status.json").read_text())["state"] == (
        "stopped"
    )
    with InstanceLock(tmp_path / "weaver.lock"):  # released on exit
        pass


def test_interrupted_stage_is_not_recorded(tmp_path):
    processor = FakeProcessor({"src_a": Interrupted()})
    runtime, stop, _, _ = make(
        tmp_path, sources=FakeSources([FakeSource("src_a")]), processor=processor
    )
    runtime.run(stop)
    assert counts(tmp_path) == {}


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


def test_damaged_archive_row_fails_alone(tmp_path):
    sources = FakeSources([FakeSource("src_ok")], raw={"src_bad": "{not json"})
    processor = FakeProcessor()
    runtime, stop, _, _ = make(tmp_path, sources=sources, processor=processor)
    runtime.run(stop)
    assert processor.seen == ["src_ok"]
    assert counts(tmp_path) == {"done": 1, "failed": 1}
    status = json.loads((tmp_path / "runtime-status.json").read_text())
    assert status["failed"] == [
        {"source_id": "src_bad", "error_class": "JSONDecodeError"}
    ]


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
        gate=gate(False, "api_key_missing"),
    )
    with caplog.at_level(logging.DEBUG):
        runtime.run(stop)
    # Ticks at t=0,5,10. Permanent: once. Waiting for LLM: not retried while
    # unavailable. Transient: retried only after its 10s backoff (t=10).
    assert processor.seen.count("src_bad") == 1
    assert processor.seen.count("src_llm") == 1
    assert processor.seen.count("src_flaky") == 2
    assert counts(tmp_path) == {"failed": 1, "retry": 1, "waiting_llm": 1}
    assert all(secret not in r.getMessage() for r in caplog.records)
    assert any(
        "llm stage skipped: api_key_missing" in r.getMessage() for r in caplog.records
    )
    status = json.loads((tmp_path / "runtime-status.json").read_text())
    assert status["llm"] == {"available": False, "reason": "api_key_missing"}
    assert status["failed"] == [{"source_id": "src_bad", "error_class": "ValueError"}]


@pytest.mark.parametrize(
    "error", [InvalidJson(), SchemaMismatch(), Refusal(), ExtractionInvalid("x")]
)
def test_contract_violation_fails_after_three(tmp_path, error):
    processor = FakeProcessor({"src_a": error})
    runtime, stop, _, clock = make(
        tmp_path, sources=FakeSources([FakeSource("src_a")]), processor=processor
    )
    for _ in range(5):  # jump past every backoff so each tick retries
        runtime._tick(stop)
        clock.now += 4000
    assert processor.seen == ["src_a"] * 3
    assert counts(tmp_path) == {"failed": 1}


@pytest.mark.parametrize(
    "error", [RateLimited(), TransientError(), LlmTimeout(), OSError()]
)
def test_transient_errors_retry_until_cap(tmp_path, error):
    from weaver.runtime.loop import MAX_RETRY_ATTEMPTS

    processor = FakeProcessor({"src_a": error})
    runtime, stop, _, clock = make(
        tmp_path, sources=FakeSources([FakeSource("src_a")]), processor=processor
    )
    for _ in range(MAX_RETRY_ATTEMPTS + 3):
        runtime._tick(stop)
        clock.now += 4000
    assert len(processor.seen) == MAX_RETRY_ATTEMPTS
    assert counts(tmp_path) == {"failed": 1}


@pytest.mark.parametrize("error", [AuthenticationError(), BudgetExceeded()])
def test_auth_or_budget_turns_llm_off_and_rechecks(tmp_path, error):
    state = {"available": True}
    probes = []

    def probe():
        probes.append(1)
        return LlmStatus(state["available"], "configured")

    llm = LlmGate(probe)
    processor = FakeProcessor({"src_a": [error, "done"]})
    runtime, stop, _, clock = make(
        tmp_path,
        sources=FakeSources([FakeSource("src_a")]),
        processor=processor,
        gate=llm,
    )
    runtime._tick(stop)
    assert counts(tmp_path) == {"waiting_llm": 1}
    assert not llm.available and llm.status.reason == error.code
    clock.now += 5
    runtime._tick(stop)  # gate off: not retried, first recheck only arms
    assert processor.seen == ["src_a"]
    clock.now += LLM_RECHECK_SECONDS
    runtime._tick(stop)  # re-probed after a minute: available again
    assert llm.available and len(probes) == 2
    assert processor.seen == ["src_a", "src_a"]
    assert counts(tmp_path) == {"done": 1}


def test_waiting_llm_sources_resume_when_llm_available(tmp_path):
    sources = FakeSources([FakeSource("src_llm")])
    first = FakeProcessor({"src_llm": "waiting_llm"})
    runtime, stop, _, _ = make(
        tmp_path, sources=sources, processor=first, gate=gate(False, "backend_none")
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


def test_insufficient_credit_waits_hourly_and_reports_reason(tmp_path):
    from weaver.llm.errors import InsufficientCredit
    from weaver.runtime.loop import CREDIT_RECHECK_SECONDS

    probes = []

    def probe():
        probes.append(1)
        return LlmStatus(True, "configured")

    llm = LlmGate(probe)
    processor = FakeProcessor({"src_a": [InsufficientCredit(), "done"]})
    runtime, stop, _, clock = make(
        tmp_path,
        sources=FakeSources([FakeSource("src_a")]),
        processor=processor,
        gate=llm,
    )
    runtime._tick(stop)
    assert counts(tmp_path) == {"waiting_llm": 1}
    status = json.loads((tmp_path / "runtime-status.json").read_text())
    assert status["llm"] == {"available": False, "reason": "insufficient_credit"}
    # No retry storm: neither the usual minute nor 59 minutes re-calls the LLM.
    for step in (5, LLM_RECHECK_SECONDS, CREDIT_RECHECK_SECONDS - 200):
        clock.now += step
        runtime._tick(stop)
    assert processor.seen == ["src_a"] and len(probes) == 1
    clock.now += CREDIT_RECHECK_SECONDS
    runtime._tick(stop)  # resolved: exactly one more processing
    assert processor.seen == ["src_a", "src_a"]
    assert counts(tmp_path) == {"done": 1}


def test_credit_error_after_recovery_still_waits_full_hour(tmp_path):
    from weaver.llm.errors import InsufficientCredit
    from weaver.runtime.loop import CREDIT_RECHECK_SECONDS

    state = {"available": False}
    llm = LlmGate(lambda: LlmStatus(state["available"], "configured"))
    sources = FakeSources()
    processor = FakeProcessor({"src_a": [InsufficientCredit(), "done"]})
    runtime, stop, _, clock = make(
        tmp_path, sources=sources, processor=processor, gate=llm
    )
    runtime._tick(stop)  # startup probe: unavailable
    state["available"] = True
    clock.now += LLM_RECHECK_SECONDS
    runtime._tick(stop)  # recovered; old check time stays far in the past
    assert llm.available
    clock.now += 2 * CREDIT_RECHECK_SECONDS
    sources.items.append(FakeSource("src_a"))
    runtime._tick(stop)
    assert not llm.available and llm.status.reason == InsufficientCredit.code
    assert processor.seen == ["src_a"]
    clock.now += 5
    runtime._tick(stop)  # must not retry five seconds after the credit error
    assert processor.seen == ["src_a"]
    clock.now += CREDIT_RECHECK_SECONDS
    runtime._tick(stop)  # an hour later it retries
    assert processor.seen == ["src_a", "src_a"]
    assert counts(tmp_path) == {"done": 1}


def test_usage_limited_waits_hourly_without_retry_storm(tmp_path):
    from weaver.llm.errors import UsageLimited
    from weaver.runtime.loop import CREDIT_RECHECK_SECONDS

    llm = LlmGate(lambda: LlmStatus(True, "configured"))
    processor = FakeProcessor({"src_a": [UsageLimited(), "done"]})
    runtime, stop, _, clock = make(
        tmp_path,
        sources=FakeSources([FakeSource("src_a")]),
        processor=processor,
        gate=llm,
    )
    runtime._tick(stop)
    # Never failed or retry: waiting with the reason visible in status.
    assert counts(tmp_path) == {"waiting_llm": 1}
    status = json.loads((tmp_path / "runtime-status.json").read_text())
    assert status["llm"] == {"available": False, "reason": "usage_limited"}
    for step in (5, LLM_RECHECK_SECONDS, CREDIT_RECHECK_SECONDS - 200):
        clock.now += step
        runtime._tick(stop)
    assert processor.seen == ["src_a"]
    clock.now += CREDIT_RECHECK_SECONDS
    runtime._tick(stop)  # released: exactly one more processing
    assert processor.seen == ["src_a", "src_a"]
    assert counts(tmp_path) == {"done": 1}


def test_usage_limited_never_becomes_failed(tmp_path):
    from weaver.llm.errors import UsageLimited
    from weaver.runtime.loop import CREDIT_RECHECK_SECONDS, MAX_RETRY_ATTEMPTS

    llm = LlmGate(lambda: LlmStatus(True, "configured"))
    processor = FakeProcessor({"src_a": [UsageLimited()]})
    runtime, stop, _, clock = make(
        tmp_path,
        sources=FakeSources([FakeSource("src_a")]),
        processor=processor,
        gate=llm,
    )
    for _ in range(MAX_RETRY_ATTEMPTS + 2):
        runtime._tick(stop)
        clock.now += CREDIT_RECHECK_SECONDS
    assert counts(tmp_path) == {"waiting_llm": 1}
