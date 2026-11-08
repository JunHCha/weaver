"""Operational log under ``log_root``; separate from Fabric's domain records.

``log_root/weaver.log`` rotates at 5 MiB keeping 5 old files (at most ~30 MiB),
each 0600. launchd's StandardOutPath/StandardErrorPath (``launchd.out.log`` and
``launchd.err.log``) only catch output before this handler is installed or from
a hard interpreter failure; uncaught exceptions are logged by type name only,
because messages and tracebacks may quote source text.
"""

import io
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

LOG_NAME = "weaver.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self) -> io.TextIOWrapper:
        # Create 0600 regardless of umask, including rotated replacements.
        fd = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
            0o600,
        )
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding or "utf-8")


class _NoTracebacks(logging.Formatter):
    def formatException(self, ei: object) -> str:  # noqa: N802 (logging API)
        return ""

    def formatStack(self, stack_info: str) -> str:  # noqa: N802 (logging API)
        return ""


def configure_logging(log_root: Path, *, level: int = logging.INFO) -> Path:
    log_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = log_root / LOG_NAME
    handler = _PrivateRotatingHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(_NoTracebacks(_FORMAT))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)

    def excepthook(
        kind: type[BaseException],
        _value: BaseException,
        _tb: TracebackType | None,
    ) -> None:
        logging.getLogger("weaver.runtime").critical("fatal: %s", kind.__name__)

    sys.excepthook = excepthook
    return path
