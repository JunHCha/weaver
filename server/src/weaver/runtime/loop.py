"""Resident foreground loop: rescan, process, maintain; one instance per state.

Every tick rescans Courier folders and the archive, so files that arrived
while the process was stopped or the Mac was asleep are recovered without
relying on file-system notifications. A wall-clock gap much longer than the
scheduled wait is reported as a resume. SIGTERM/SIGINT stop between Sources:
the Source in flight finishes and is recorded before the process exits.

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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal, Protocol

from weaver.courier.atomic import write_bytes_atomic

from .ledger import RuntimeLedger

logger = logging.getLogger("weaver.runtime")

type Outcome = Literal["done", "waiting_llm"]

EXIT_ALREADY_RUNNING = 3
MAX_TICK_BACKOFF_SECONDS = 300.0
MAX_RETRY_BACKOFF_SECONDS = 3600.0
RESUME_SLACK_SECONDS = 60.0
LOCK_NAME = "weaver.lock"
STATUS_NAME = "runtime-status.json"


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


class SourceFeed[S: HasSourceId](Protocol):
    def sources(self) -> Sequence[S]: ...


class SourceProcessor[S: HasSourceId](Protocol):
    def process(self, source: S) -> Outcome:
        """Return ``done`` or ``waiting_llm``.

        Raise ValueError for permanent input/contract failures; any other
        exception is retried after a backoff.
        """
        ...


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


@dataclass(frozen=True)
class LlmStatus:
    available: bool
    reason: str


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
        sources: SourceFeed[S],
        processor: SourceProcessor[S],
        ledger: RuntimeLedger,
        llm: LlmStatus,
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
        self.processor = processor
        self.ledger = ledger
        self.llm = llm
        self.interval = interval
        self.publisher = publisher or NoPublisher()
        self.maintenance = list(maintenance)
        self.clock = clock
        self._wait = wait
        self.resumes = 0
        self.consecutive_failures = 0
        self._last_run: dict[str, float] = {}
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
        if not self.llm.available:
            logger.warning("llm stage skipped: %s", self.llm.reason)
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

    def _process_sources(self, stop: threading.Event) -> None:
        progress = self.ledger.progress()
        for source in self.feed.sources():
            if stop.is_set():
                return
            sid = source.source_id
            known = progress.get(sid)
            if known is not None:
                if known.status in ("done", "failed"):
                    continue
                if known.status == "waiting_llm" and not self.llm.available:
                    continue
                if known.status == "retry" and (
                    known.next_attempt_at is not None
                    and known.next_attempt_at > self.clock()
                ):
                    continue
            self._process_one(source, known.attempts if known else 0)

    def _process_one(self, source: S, attempts: int) -> None:
        sid = source.source_id
        try:
            outcome = self.processor.process(source)
        except ValueError as error:
            self.ledger.record(sid, "failed", error_class=type(error).__name__)
            logger.warning("source failed: %s %s", sid, type(error).__name__)
        except Exception as error:
            delay = min(
                self.interval * 2.0 ** (attempts + 1), MAX_RETRY_BACKOFF_SECONDS
            )
            self.ledger.record(
                sid,
                "retry",
                error_class=type(error).__name__,
                next_attempt_at=self.clock() + delay,
            )
            logger.warning(
                "source retry in %.0fs: %s %s", delay, sid, type(error).__name__
            )
        else:
            self.ledger.record(sid, outcome)
            logger.info("source %s: %s", outcome, sid)

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
            "llm": {"available": self.llm.available, "reason": self.llm.reason},
            "resumes": self.resumes,
            "consecutive_failures": self.consecutive_failures,
            "sources": self.ledger.counts(),
        }
        data = json.dumps(status, ensure_ascii=True, indent=2).encode()
        write_bytes_atomic(self.state_root / STATUS_NAME, data)
