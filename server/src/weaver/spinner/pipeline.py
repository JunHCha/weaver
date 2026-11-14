"""One-shot text pipeline with private, durable checkpoints and atomic results.

Cache keys combine caller versions with built-in and optional adapter versions.
"""

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from weaver.config import ServerConfig, SpinnerSettings
from weaver.llm import LlmError
from weaver.loom.call import GalaxyCallParser
from weaver.schema import Script, Source
from weaver.spinner.reviser import (
    FORMAT_VERSION,
    PROMPT_VERSION,
    ReviseHints,
    Reviser,
    parse_revision,
    revision_to_thread,
)
from weaver.spinner.text import PARSER_VERSION, SourceLocator, text_to_thread

_LOG = logging.getLogger(__name__)


class SpinnerError(ValueError):
    """Permanent unsupported input or adapter-contract error."""


class SpinnerTransientError(Exception):
    """Only this explicit adapter signal requests Spinner-level retries."""


class TextParser(Protocol):
    """Galaxy call-script (#56) and replaceable channel parser boundary."""

    # Optional at runtime; unversioned adapters rely on the caller version.
    @property
    def version(self) -> str | None:
        return None

    def parse(self, source: Source, text: str) -> Script: ...


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
        revision_threshold: int = 0,
        settings: SpinnerSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not configuration_version.strip():
            raise ValueError("configuration_version must be nonempty")
        if revision_threshold < 0:
            raise ValueError("revision_threshold must not be negative")
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = (state_root / "spinner.sqlite3").resolve()
        self._store = store
        self._timezone = timezone or ZoneInfo("Asia/Seoul")
        self._parsers: dict[str, TextParser] = {"call": GalaxyCallParser()}
        self._parsers.update(parsers or {})
        self._reviser = reviser
        # Calls at most this long skip revision here: Loom corrects them while
        # summarizing, in the same LLM call.
        self._threshold = revision_threshold
        fingerprint = json.dumps(
            [
                configuration_version,
                self._timezone.key,
                PARSER_VERSION,
                PROMPT_VERSION,
                FORMAT_VERSION,
                sorted(
                    (channel, getattr(parser, "version", None))
                    for channel, parser in self._parsers.items()
                ),
                getattr(reviser, "version", None),
                revision_threshold,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        self._version = f"{configuration_version}:{hashlib.sha256(fingerprint.encode()).hexdigest()}"
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
            self._recover(db)

    @staticmethod
    def _recover(db: sqlite3.Connection) -> None:
        # Called only while holding flock: no live owner can be interrupted.
        db.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")

    @classmethod
    def from_config(
        cls,
        store: SourceLocator,
        config: ServerConfig,
        *,
        configuration_version: str,
        parsers: Mapping[str, TextParser] | None = None,
        reviser: Reviser | None = None,
        revision_threshold: int = 0,
    ) -> "Spinner":
        return cls(
            store,
            config.state_root,
            configuration_version=configuration_version,
            timezone=config.time.zone,
            settings=config.spinner.retry,
            parsers=parsers,
            reviser=reviser,
            revision_threshold=revision_threshold,
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
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise SpinnerError("state_requires_wal")
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

    def _finish(
        self,
        record: int,
        started: float,
        error: BaseException | None,
        *,
        source: Source | None = None,
        result: Script | None = None,
        field: Literal["parsed", "thread"] | None = None,
    ) -> None:
        elapsed = max(0.0, self._clock() - started)
        # Never store exception messages: injected adapters may include content.
        category = (
            "interrupted"
            if error is not None and not isinstance(error, Exception)
            else "llm"
            if isinstance(error, LlmError)
            else "transient"
            if isinstance(error, SpinnerTransientError)
            else "permanent"
            if error is not None
            else None
        )
        status = (
            "interrupted"
            if category == "interrupted"
            else "failed"
            if error
            else "succeeded"
        )
        with self._connect() as db:
            if source is not None and result is not None and field is not None:
                self._save_result(db, source, result, field)
            db.execute(
                "UPDATE attempts SET status=?,error_class=?,elapsed_seconds=? WHERE id=?",
                (status, category, elapsed, record),
            )
        _LOG.info(
            "spinner record=%d status=%s elapsed_seconds=%f",
            record,
            category or "succeeded",
            elapsed,
        )

    def _save_result(
        self,
        db: sqlite3.Connection,
        source: Source,
        result: Script,
        field: Literal["parsed", "thread"],
    ) -> None:
        db.execute(
            f"UPDATE results SET {field}=? WHERE source_id=? AND version=?",
            (result.model_dump_json(), source.source_id, self._version),
        )

    def _run(
        self,
        source: Source,
        attempt: int,
        stage: str,
        operation: Callable[[], Script],
        *,
        field: Literal["parsed", "thread"] | None = None,
    ) -> Script:
        record = self._start(source, attempt, stage)
        started = self._clock()
        try:
            try:
                result = operation()
                # Revalidate injected adapters, including unchecked model_copy output.
                result = Script.model_validate(result.model_dump())
            except ValidationError:
                raise SpinnerError("invalid_adapter_thread") from None
            if (result.source_id, result.channel, result.occurred_at) != (
                source.source_id,
                source.channel,
                source.occurred_at,
            ):
                raise SpinnerError("thread_provenance_mismatch")
        except BaseException as error:
            self._finish(record, started, error)
            raise
        self._finish(record, started, None, source=source, result=result, field=field)
        return result

    def _parse(self, source: Source) -> Script:
        if source.kind != "text":
            raise SpinnerError("unsupported_kind: fleece STT is deferred")
        if source.channel not in {"kakao", "share_text", "call"}:
            raise SpinnerError("unsupported_text_channel")
        if source.channel == "call" and source.channel not in self._parsers:
            raise SpinnerError("call_parser_not_configured")
        archived = self._store.locate(source.source_id)
        if archived.source != source:
            raise SpinnerError("archive_source_mismatch")
        fd = os.open(
            archived.payload_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
        with os.fdopen(fd, "rb") as stream:
            payload = stream.read()
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

    def _revise(self, source: Source, parsed: Script) -> Script:
        if self._reviser is None:
            raise SpinnerError("call_reviser_not_configured")
        serialized = self._reviser.revise(
            parsed, ReviseHints(counterpart=source.hints.counterpart)
        )
        return revision_to_thread(parse_revision(serialized), parsed)

    def was_revised(self, source: Source) -> bool:
        """Whether this version's revision step committed for the source."""
        with self._lock(), self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM attempts WHERE source_id=? AND version=? "
                "AND stage='revise' AND status='succeeded' LIMIT 1",
                (source.source_id, self._version),
            ).fetchone()
        return row is not None

    def save_revised(self, source: Source, script: Script) -> None:
        """Replace the stored result with a Script corrected elsewhere (by Loom)."""
        script = Script.model_validate(script.model_dump())
        if (script.source_id, script.channel, script.occurred_at) != (
            source.source_id,
            source.channel,
            source.occurred_at,
        ):
            raise SpinnerError("thread_provenance_mismatch")
        with self._lock(), self._connect() as db:
            self._save_result(db, source, script, "thread")

    def spin(self, source: Source, *, reprocess: bool = False) -> Script:
        """Return only a committed complete Script, preserving failed checkpoints."""
        with self._lock():
            with self._connect() as db:
                self._recover(db)
            return self._run(
                source, 0, "spin", partial(self._spin_locked, source, reprocess)
            )

    def _spin_locked(self, source: Source, reprocess: bool) -> Script:
        if source.channel == "call":
            if "call" not in self._parsers:
                raise SpinnerError("call_parser_not_configured")
            if self._reviser is None and self._threshold == 0:
                raise SpinnerError("call_reviser_not_configured")
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
                return Script.model_validate_json(row[2])
            db.execute(
                "INSERT OR IGNORE INTO results(source_id,version,source) VALUES(?,?,?)",
                (*key, source.model_dump_json()),
            )
        parsed = Script.model_validate_json(row[1]) if row and row[1] else None
        delay = self._settings.backoff_seconds
        for attempt in range(1, self._settings.max_attempts + 1):
            total = self._start(source, attempt, "total")
            started = self._clock()
            try:
                if parsed is None:
                    parsed = self._run(
                        source,
                        attempt,
                        "parse",
                        lambda: self._parse(source),
                        field="parsed",
                    )
                result = parsed
                if source.channel == "call" and len(parsed.text) > self._threshold:
                    result = self._run(
                        source,
                        attempt,
                        "revise",
                        partial(self._revise, source, parsed),
                        field="thread",
                    )
                else:
                    with self._connect() as db:
                        self._save_result(db, source, result, "thread")
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
