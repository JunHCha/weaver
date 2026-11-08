"""One-shot text pipeline with private, durable checkpoints and atomic results.

Configuration versions are caller-owned engine/model/prompt identifiers. Change
that identifier whenever an injected parser or reviser changes its behavior.
"""

import fcntl
import hashlib
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from weaver.config import ServerConfig, SpinnerSettings
from weaver.llm import LlmError
from weaver.schema.models import Source, Thread
from weaver.spinner.reviser import (
    ReviseHints,
    Reviser,
    parse_revision,
    revision_to_thread,
)
from weaver.spinner.text import SourceLocator, text_to_thread

_LOG = logging.getLogger(__name__)


class SpinnerError(ValueError):
    """Permanent unsupported input or adapter-contract error."""


class SpinnerTransientError(Exception):
    """Only this explicit adapter signal requests Spinner-level retries."""


class TextParser(Protocol):
    """Galaxy call-script (#56) and replaceable channel parser boundary."""

    def parse(self, source: Source, text: str) -> Thread: ...


class Spinner:
    def __init__(
        self,
        store: SourceLocator,
        state_root: Path,
        *,
        configuration_version: str,
        timezone: ZoneInfo | None = None,
        parsers: Mapping[str, TextParser] | None = None,
        reviser: Reviser | None = None,
        settings: SpinnerSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not configuration_version.strip():
            raise ValueError("configuration_version must be nonempty")
        self.path = (state_root / "spinner.sqlite3").resolve()
        self._store = store
        self._version = configuration_version
        self._timezone = timezone or ZoneInfo("Asia/Seoul")
        self._parsers = dict(parsers or {})
        self._reviser = reviser
        self._settings = settings or SpinnerSettings()
        self._clock = clock
        self._sleep = sleep
        with self._lock(), self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS results (
                    source_id TEXT NOT NULL, version TEXT NOT NULL,
                    source TEXT NOT NULL, parsed TEXT, thread TEXT,
                    PRIMARY KEY(source_id, version)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL,
                    version TEXT NOT NULL, attempt INTEGER NOT NULL,
                    stage TEXT NOT NULL, status TEXT NOT NULL,
                    error_class TEXT, elapsed_seconds REAL
                );
            """)
            # A process killed during a stage leaves durable evidence of the
            # interrupted attempt; its elapsed time is deliberately unknown.
            db.execute(
                "UPDATE attempts SET status='interrupted' WHERE status='running'"
            )

    @classmethod
    def from_config(
        cls,
        store: SourceLocator,
        config: ServerConfig,
        *,
        configuration_version: str,
        parsers: Mapping[str, TextParser] | None = None,
        reviser: Reviser | None = None,
    ) -> "Spinner":
        return cls(
            store,
            config.state_root,
            configuration_version=configuration_version,
            timezone=config.time.zone,
            settings=config.spinner.retry,
            parsers=parsers,
            reviser=reviser,
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        fd = os.open(
            str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600
        )
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm", "-journal"):
            with suppress(FileNotFoundError):
                os.chmod(f"{self.path}{suffix}", 0o600)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("PRAGMA checkpoint_fullfsync=ON")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _start(self, source: Source, attempt: int, stage: str) -> int:
        _LOG.info(
            "spinner source_id=%s version=%s attempt=%d stage=%s",
            source.source_id,
            self._version,
            attempt,
            stage,
        )
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO attempts(source_id,version,attempt,stage,status) VALUES(?,?,?,?,?)",
                (source.source_id, self._version, attempt, stage, "running"),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def _finish(self, record: int, started: float, error: BaseException | None) -> None:
        elapsed = max(0.0, self._clock() - started)
        # Never store exception messages: injected adapters may include content.
        category = (
            "llm"
            if isinstance(error, LlmError)
            else "transient"
            if isinstance(error, SpinnerTransientError)
            else "permanent"
            if error is not None
            else None
        )
        with self._connect() as db:
            db.execute(
                "UPDATE attempts SET status=?,error_class=?,elapsed_seconds=? WHERE id=?",
                ("failed" if error else "succeeded", category, elapsed, record),
            )
        _LOG.info(
            "spinner record=%d status=%s elapsed_seconds=%f",
            record,
            category or "succeeded",
            elapsed,
        )

    def _run(
        self, source: Source, attempt: int, stage: str, operation: Callable[[], Thread]
    ) -> Thread:
        record = self._start(source, attempt, stage)
        started = self._clock()
        try:
            result = operation()
            # Revalidate injected adapters, including unchecked model_copy output.
            result = Thread.model_validate(result.model_dump())
            if (result.source_id, result.channel, result.occurred_at) != (
                source.source_id,
                source.channel,
                source.occurred_at,
            ):
                raise SpinnerError("thread_provenance_mismatch")
        except BaseException as error:
            self._finish(record, started, error)
            raise
        self._finish(record, started, None)
        return result

    def _parse(self, source: Source) -> Thread:
        if source.kind != "text":
            raise SpinnerError("unsupported_kind: fleece STT is deferred")
        if source.channel not in {"kakao", "share_text", "call"}:
            raise SpinnerError("unsupported_text_channel")
        if source.channel == "call" and source.channel not in self._parsers:
            raise SpinnerError("call_parser_not_configured")
        archived = self._store.locate(source.source_id)
        if archived.source != source:
            raise SpinnerError("archive_source_mismatch")
        payload = archived.payload_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != source.sha256:
            raise SpinnerError("payload_mismatch")
        try:
            text = payload.decode("utf-8")
        except UnicodeError:
            raise SpinnerError("unsupported_encoding") from None
        if "\x00" in text:
            raise SpinnerError("unsupported_encoding")
        parser = self._parsers.get(source.channel)
        if parser is not None:
            return parser.parse(source, text)
        return text_to_thread(source, text, timezone=self._timezone)

    def _revise(self, source: Source, parsed: Thread) -> Thread:
        if self._reviser is None:
            raise SpinnerError("call_reviser_not_configured")
        serialized = self._reviser.revise(
            parsed, ReviseHints(counterpart=source.hints.counterpart)
        )
        return revision_to_thread(parse_revision(serialized), parsed)

    def spin(self, source: Source, *, reprocess: bool = False) -> Thread:
        """Return only a committed complete Thread, preserving failed checkpoints."""
        with self._lock():
            return self._run(
                source, 0, "spin", partial(self._spin_locked, source, reprocess)
            )

    def _spin_locked(self, source: Source, reprocess: bool) -> Thread:
        key = (source.source_id, self._version)
        with self._connect() as db:
            row = db.execute(
                "SELECT source,parsed,thread FROM results WHERE source_id=? AND version=?",
                key,
            ).fetchone()
            if row and Source.model_validate_json(row[0]) != source:
                raise SpinnerError("source_identity_mismatch")
            if reprocess:
                db.execute("DELETE FROM results WHERE source_id=? AND version=?", key)
                row = None
            if row and row[2] is not None:
                return Thread.model_validate_json(row[2])
            db.execute(
                "INSERT OR IGNORE INTO results(source_id,version,source) VALUES(?,?,?)",
                (*key, source.model_dump_json()),
            )
        parsed = Thread.model_validate_json(row[1]) if row and row[1] else None
        delay = self._settings.backoff_seconds
        for attempt in range(1, self._settings.max_attempts + 1):
            total = self._start(source, attempt, "total")
            started = self._clock()
            try:
                if parsed is None:
                    parsed = self._run(
                        source, attempt, "parse", lambda: self._parse(source)
                    )
                    with self._connect() as db:
                        db.execute(
                            "UPDATE results SET parsed=? WHERE source_id=? AND version=?",
                            (parsed.model_dump_json(), *key),
                        )
                result = parsed
                if source.channel == "call":
                    result = self._run(
                        source,
                        attempt,
                        "revise",
                        partial(self._revise, source, parsed),
                    )
                with self._connect() as db:
                    db.execute(
                        "UPDATE results SET thread=? WHERE source_id=? AND version=?",
                        (result.model_dump_json(), *key),
                    )
            except BaseException as error:
                self._finish(total, started, error)
                if (
                    isinstance(error, LlmError)
                    or not isinstance(error, SpinnerTransientError)
                    or attempt == self._settings.max_attempts
                ):
                    raise
                self._sleep(delay)
                delay = min(delay * 2, self._settings.max_backoff_seconds)
            else:
                self._finish(total, started, None)
                return result
        raise AssertionError("unreachable")
