"""Atomic publication of one file at a time in a Courier folder.

The content is written to a dot-prefixed temporary file in the destination
directory, flushed, and then moved over the final name with ``os.replace``.
Consumers ignore dot files, so an interrupted write never exposes partial JSON
under a final name. This is per-file atomicity, not a multi-file transaction.
"""

import contextlib
import os
import secrets
import sys
from pathlib import Path

from pydantic import BaseModel

# macOS fsync() stops at the drive's volatile cache; F_FULLFSYNC flushes it.
_F_FULLFSYNC: int | None = None
if sys.platform == "darwin":
    import fcntl

    _F_FULLFSYNC = getattr(fcntl, "F_FULLFSYNC", 51)


def full_fsync(fd: int) -> None:
    """Flush to stable storage: F_FULLFSYNC on macOS, else (or if refused) fsync."""
    if sys.platform == "darwin" and _F_FULLFSYNC is not None:
        import fcntl

        try:
            fcntl.fcntl(fd, _F_FULLFSYNC)
            return
        except OSError:
            pass  # e.g. a filesystem without F_FULLFSYNC; fall back below.
    os.fsync(fd)


def fsync_directory(directory: Path, *, strict: bool = False) -> None:
    """Make directory entries durable. ``strict`` propagates failures."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        if strict:
            raise
        return
    try:
        full_fsync(fd)
    except OSError:
        if strict:
            raise
        # Some filesystems do not support directory fsync.
    finally:
        os.close(fd)


def temporary_name(final: Path) -> Path:
    """Hidden sibling name; consumers on both devices skip dot files."""
    return final.with_name(f".{final.name}.{secrets.token_hex(8)}.tmp")


def write_bytes_atomic(final: Path, data: bytes, *, mode: int | None = None) -> None:
    """Publish ``data`` at ``final`` or leave the previous state untouched.

    The file is 0600 by default; ``mode`` sets exact permission bits instead.
    """
    temporary = temporary_name(final)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            full_fsync(stream.fileno())
        os.replace(temporary, final)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    fsync_directory(final.parent)


def write_json_atomic(final: Path, model: BaseModel) -> None:
    """Serialize a wire model (by alias) and publish it atomically."""
    write_bytes_atomic(final, model.model_dump_json().encode("utf-8"))
