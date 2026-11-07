"""Atomic publication of one file at a time in a Courier folder.

The content is written to a dot-prefixed temporary file in the destination
directory, flushed, and then moved over the final name with ``os.replace``.
Consumers ignore dot files, so an interrupted write never exposes partial JSON
under a final name. This is per-file atomicity, not a multi-file transaction.
"""

import contextlib
import os
import secrets
from pathlib import Path

from pydantic import BaseModel


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # Some filesystems do not support directory fsync.
    finally:
        os.close(fd)


def temporary_name(final: Path) -> Path:
    """Hidden sibling name; consumers on both devices skip dot files."""
    return final.with_name(f".{final.name}.{secrets.token_hex(8)}.tmp")


def write_bytes_atomic(final: Path, data: bytes) -> None:
    """Publish ``data`` at ``final`` or leave the previous state untouched."""
    temporary = temporary_name(final)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, final)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    _fsync_directory(final.parent)


def write_json_atomic(final: Path, model: BaseModel) -> None:
    """Serialize a wire model (by alias) and publish it atomically."""
    write_bytes_atomic(final, model.model_dump_json().encode("utf-8"))
