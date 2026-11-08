"""Resident foreground loop: rescan, process, maintain; one instance per state.

Every tick rescans Courier folders and the archive, so files that arrived
while the process was stopped or the Mac was asleep are recovered without
relying on file-system notifications. A wall-clock gap much longer than the
scheduled wait is reported as a resume. SIGTERM/SIGINT are checked before each
LLM/journal stage: a stage already running finishes, the rest is left for the
next start, and nothing is recorded as failed.

Failure classes (per Source):
- ValueError (invalid archive row, unsupported input): ``failed`` at once.
- LLM contract violations (InvalidJson, SchemaMismatch, MissingOutput,
  Refusal, invalid extraction output): retried, ``failed`` after 3.
- AuthenticationError, BudgetExceeded, LlmUnavailable: ``waiting_llm``; the
  LLM gate turns off and is re-probed every minute.
- InsufficientCredit: the same, but re-probed hourly (a restart retries at
  once) so an empty account is not hammered.
- Anything else (RateLimited, TransientError, LlmTimeout, OSError, ...):
  ``retry`` with exponential backoff, ``failed`` after 20 attempts.

Logs carry only opaque IDs, counts and exception type names, never content.
"""

import contextlib
import fcntl
import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal, Protocol

from weaver.courier.atomic import write_bytes_atomic
from weaver.llm.errors import (
    AuthenticationError,
    BudgetExceeded,
    InsufficientCredit,
    InvalidJson,
    MissingOutput,
    Refusal,
    SchemaMismatch,
)

from .gate import ExtractionInvalid, Interrupted, LlmGate, LlmUnavailable
from .ledger import Progress, RuntimeLedger

logger = logging.getLogger("weaver.runtime")

type Outcome = Literal["done", "waiting_llm"]

EXIT_ALREADY_RUNNING = 3
MAX_TICK_BACKOFF_SECONDS = 300.0
MAX_RETRY_BACKOFF_SECONDS = 3600.0
MAX_RETRY_ATTEMPTS = 20
MAX_CONTRACT_FAILURES = 3
LLM_RECHECK_SECONDS = 60.0
CREDIT_RECHECK_SECONDS = 3600.0
RESUME_SLACK_SECONDS = 60.0
LOCK_NAME = "weaver.lock"
STATUS_NAME = "runtime-status.json"

_CONTRACT = (InvalidJson, SchemaMismatch, MissingOutput, Refusal, ExtractionInvalid)
_LLM_OFF = (AuthenticationError, BudgetExceeded, InsufficientCredit, LlmUnavailable)


class AlreadyRunning(Exception):
    """Another process holds this state_root's instance lock."""


class InstanceLock:
    """Non-blocking advisory flock; released by the kernel if the process dies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "InstanceLock":
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning() from None
        except BaseException:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            os.close(self._fd)  # releases the flock
            self._fd = None


class Scanner(Protocol):
    def scan_once(self) -> object: ...


class HasSourceId(Protocol):
    @property
    def source_id(self) -> str: ...


class SourceRows(Protocol):
    def source_rows(self, exclude: Collection[str] = ()) -> list[tuple[str, str]]:
        """(source_id, unvalidated JSON) in arrival order, minus ``exclude``."""
        ...


class SourceProcessor[S: HasSourceId](Protocol):
    def process(self, source: S, stop: threading.Event) -> Outcome: ...


class VaultPublisher(Protocol):
    """#7 Publisher boundary: write notes changed since the last call."""

    def publish(self) -> None: ...


class NoPublisher:
    def publish(self) -> None:
        return None


@dataclass(frozen=True)
class MaintenanceTask:
    """Periodic job such as #27 feedback purge; runs at startup, then per interval."""

    name: str
    interval_seconds: float
    run: Callable[[], object]


@contextlib.contextmanager
def stop_on_signals(stop: threading.Event) -> Iterator[None]:
    def handler(signum: int, _frame: FrameType | None) -> None:
        logger.info("stop requested: %s", signal.Signals(signum).name)
        stop.set()

    previous = {
        sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


class Runtime[S: HasSourceId]:
    def __init__(
        self,
        *,
        state_root: Path,
        scanner: Scanner,
        sources: SourceRows,
        decode: Callable[[str], S],
        processor: SourceProcessor[S],
        ledger: RuntimeLedger,
        gate: LlmGate,
        interval: float,
        publisher: VaultPublisher | None = None,
        maintenance: Sequence[MaintenanceTask] = (),
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        if not interval > 0:
            raise ValueError("interval must be positive")
        self.state_root = state_root
        self.scanner = scanner
        self.feed = sources
        self.decode = decode
        self.processor = processor
        self.ledger = ledger
        self.gate = gate
        self.interval = interval
        self.publisher = publisher or NoPublisher()
        self.maintenance = list(maintenance)
        self.clock = clock
        self._wait = wait
        self.resumes = 0
        self.consecutive_failures = 0
        self._last_run: dict[str, float] = {}
        self._last_llm_check: float | None = None
        self._started_at = datetime.now(UTC).isoformat()

    # ---- process lifecycle -----------------------------------------------------

    def serve(self, *, acquire_lock: bool = True) -> int:
        """Run until SIGTERM/SIGINT. Exit code 3 if another instance runs.

        Pass ``acquire_lock=False`` only while already holding InstanceLock
        (the CLI takes it before opening any store).
        """
        lock: contextlib.AbstractContextManager[object] = (
            InstanceLock(self.state_root / LOCK_NAME)
            if acquire_lock
            else contextlib.nullcontext()
        )
        try:
            with lock:
                stop = threading.Event()
                with stop_on_signals(stop):
                    logger.info("weaver started: pid %d", os.getpid())
                    self.run(stop)
                self._write_status("stopped")
                logger.info("weaver stopped")
                return 0
        except AlreadyRunning:
            logger.error("another weaver instance holds the state lock; exiting")
            return EXIT_ALREADY_RUNNING

    def run(self, stop: threading.Event) -> None:
        if not self.gate.available:
            logger.warning("llm stage skipped: %s", self.gate.status.reason)
        wait = self._wait or stop.wait
        expected: float | None = None
        last: float | None = None
        while not stop.is_set():
            now = self.clock()
            if last is not None and expected is not None:
                gap = now - last
                if gap > expected + RESUME_SLACK_SECONDS:
                    self.resumes += 1
                    logger.info("resume detected after %.0fs; rescanning", gap)
            last = now
            delay = self._tick(stop)
            expected = delay
            if wait(delay):
                break

    # ---- one pass ----------------------------------------------------------------

    def _tick(self, stop: threading.Event) -> float:
        try:
            self._recheck_llm()
            self.scanner.scan_once()
            self._process_sources(stop)
            if not stop.is_set():
                self.publisher.publish()
                self._run_maintenance()
        except Exception as error:
            self.consecutive_failures += 1
            delay = min(
                self.interval * 2.0**self.consecutive_failures,
                MAX_TICK_BACKOFF_SECONDS,
            )
            logger.error(
                "tick failed: %s (consecutive %d, next in %.0fs)",
                type(error).__name__,
                self.consecutive_failures,
                delay,
            )
            self._safe_status("degraded")
            return delay
        self.consecutive_failures = 0
        self._safe_status("running")
        return self.interval

    def _recheck_llm(self) -> None:
        if self.gate.available:
            return
        now = self.clock()
        recheck = (
            CREDIT_RECHECK_SECONDS
            if self.gate.status.reason == InsufficientCredit.code
            else LLM_RECHECK_SECONDS
        )
        if self._last_llm_check is not None and now - self._last_llm_check < recheck:
            return
        if self._last_llm_check is not None:  # the startup probe already ran
            if self.gate.refresh().available:
                logger.info("llm available again; resuming waiting sources")
        self._last_llm_check = now

    def _process_sources(self, stop: threading.Event) -> None:
        progress = self.ledger.progress()
        for sid, raw in self.feed.source_rows(exclude=self.ledger.final_ids()):
            if stop.is_set():
                return
            known = progress.get(sid)
            if known is not None:
                if known.status == "waiting_llm" and not self.gate.available:
                    continue
                if known.status == "retry" and (
                    known.next_attempt_at is not None
                    and known.next_attempt_at > self.clock()
                ):
                    continue
            self._process_one(sid, raw, known, stop)

    def _process_one(
        self, sid: str, raw: str, known: Progress | None, stop: threading.Event
    ) -> None:
        attempts = (known.attempts if known else 0) + 1
        contract = known.contract_failures if known else 0
        try:
            source = self.decode(raw)
            if source.source_id != sid:
                raise ValueError("source_id mismatch")
            outcome = self.processor.process(source, stop)
        except Interrupted:
            return  # stop requested before the next stage; nothing failed
        except _LLM_OFF as error:
            reason = getattr(error, "code", type(error).__name__)
            if self.gate.available:
                self.gate.disable(reason)
                # Restart the recheck interval from this failure, not from an
                # older recovery, so the wait is honoured (credit: one hour).
                self._last_llm_check = self.clock()
                logger.warning("llm disabled: %s", reason)
            self.ledger.record(
                sid,
                "waiting_llm",
                attempts=attempts,
                contract_failures=contract,
                error_class=type(error).__name__,
            )
        except _CONTRACT as error:
            contract += 1
            self._retry_or_fail(
                sid, error, attempts, contract, contract >= MAX_CONTRACT_FAILURES
            )
        except ValueError as error:
            self.ledger.record(
                sid,
                "failed",
                attempts=attempts,
                contract_failures=contract,
                error_class=type(error).__name__,
            )
            logger.warning("source failed: %s %s", sid, type(error).__name__)
        except Exception as error:
            self._retry_or_fail(
                sid, error, attempts, contract, attempts >= MAX_RETRY_ATTEMPTS
            )
        else:
            self.ledger.record(
                sid, outcome, attempts=attempts, contract_failures=contract
            )
            logger.info("source %s: %s", outcome, sid)

    def _retry_or_fail(
        self, sid: str, error: Exception, attempts: int, contract: int, give_up: bool
    ) -> None:
        name = type(error).__name__
        if give_up:
            self.ledger.record(
                sid,
                "failed",
                attempts=attempts,
                contract_failures=contract,
                error_class=name,
            )
            logger.warning(
                "source failed after %d attempts: %s %s", attempts, sid, name
            )
            return
        delay = min(self.interval * 2.0**attempts, MAX_RETRY_BACKOFF_SECONDS)
        self.ledger.record(
            sid,
            "retry",
            attempts=attempts,
            contract_failures=contract,
            error_class=name,
            next_attempt_at=self.clock() + delay,
        )
        logger.warning("source retry in %.0fs: %s %s", delay, sid, name)

    def _run_maintenance(self) -> None:
        now = self.clock()
        for task in self.maintenance:
            last = self._last_run.get(task.name)
            if last is not None and now - last < task.interval_seconds:
                continue
            self._last_run[task.name] = now
            try:
                task.run()
            except Exception as error:
                logger.error(
                    "maintenance failed: %s %s", task.name, type(error).__name__
                )

    # ---- status ------------------------------------------------------------------

    def _safe_status(self, state: str) -> None:
        try:
            self._write_status(state)
        except Exception as error:
            logger.error("status write failed: %s", type(error).__name__)

    def _write_status(self, state: str) -> None:
        status = {
            "state": state,
            "pid": os.getpid(),
            "started_at": self._started_at,
            "updated_at": datetime.now(UTC).isoformat(),
            "llm": {
                "available": self.gate.available,
                "reason": self.gate.status.reason,
            },
            "resumes": self.resumes,
            "consecutive_failures": self.consecutive_failures,
            "sources": self.ledger.counts(),
            "failed": self.ledger.failures(),
        }
        data = json.dumps(status, ensure_ascii=True, indent=2).encode()
        write_bytes_atomic(self.state_root / STATUS_NAME, data)
