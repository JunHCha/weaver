"""File names, ignore rules and bounded, fail-closed JSON reading."""

import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from weaver.schema.models import ULID

MAX_JSON_BYTES = 1 << 20
CONFLICT_MARKER = ".sync-conflict-"

MANIFEST_NAME = re.compile(rf"^(cap_{ULID})\.json$")
REVISION_NAME = re.compile(rf"^((?:prp_{ULID}|[a-zA-Z0-9_-]+))\.r([1-9][0-9]*)\.json$")
ALERT_NAME = re.compile(rf"^alert_(alt_{ULID})\.json$")
# ".<final name>.<16 hex>.tmp" from atomic.temporary_name, for receipts,
# proposals and alerts only (the files the server itself publishes).
OWN_TEMPORARY = re.compile(
    rf"^\.(?:cap_{ULID}\.json|[a-zA-Z0-9_-]+\.r[1-9][0-9]*\.json"
    rf"|alert_alt_{ULID}\.json)\.[0-9a-f]{{16}}\.tmp$"
)


def is_ignored(name: str) -> bool:
    """Syncthing temporaries (``.syncthing.*.tmp``, ``~syncthing~*``), our own
    atomic-write temporaries and every other dot file are never application data.
    """
    return name.startswith(".") or name.startswith("~syncthing~")


def is_conflict(name: str) -> bool:
    """Syncthing conflict copies are preserved for a human, never processed."""
    return CONFLICT_MARKER in name


class CourierFileError(Exception):
    """A file cannot be processed now. ``code`` is safe to log; no content is."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RevisionName:
    proposal_id: str
    rev: int


def parse_revision_name(name: str) -> RevisionName | None:
    match = REVISION_NAME.fullmatch(name)
    if match is None:
        return None
    return RevisionName(match.group(1), int(match.group(2)))


def revision_file_name(proposal_id: str, rev: int) -> str:
    return f"{proposal_id}.r{rev}.json"


_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_regular(name: str | Path, dir_fd: int | None = None) -> int:
    """Open without following a final symlink or blocking on a FIFO.

    The type is checked on the opened descriptor, so a swap between a
    directory listing and the open cannot redirect the read.
    """
    try:
        fd = os.open(name, _OPEN_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise CourierFileError("missing") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise CourierFileError("symlink") from None
        raise CourierFileError("unreadable") from None
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        os.close(fd)
        raise CourierFileError("unreadable") from None
    if not stat.S_ISREG(mode):
        os.close(fd)
        raise CourierFileError("not_regular_file")
    return fd


def read_regular_bytes(path: Path, limit: int = MAX_JSON_BYTES) -> bytes:
    """Read a regular non-symlink file, failing closed on anything else."""
    fd = open_regular(path)
    try:
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        raise CourierFileError("unreadable") from None
    if len(data) > limit:
        raise CourierFileError("too_large")
    return data


def is_own_temporary(name: str) -> bool:
    """A leftover of ``atomic.write_bytes_atomic`` for a server-written file."""
    return OWN_TEMPORARY.fullmatch(name) is not None


def load_versioned[M: BaseModel](path: Path, model: type[M], *, version: int = 1) -> M:
    """Parse one wire file. Unknown versions are reported, never interpreted."""
    data = read_regular_bytes(path)
    try:
        raw: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CourierFileError("invalid_json") from None
    if not isinstance(raw, dict):
        raise CourierFileError("invalid_schema")
    actual_version = raw.get("schema_version")
    if type(actual_version) is int and actual_version != version:
        raise CourierFileError("unknown_version")
    try:
        # JSON mode keeps integer-token strictness (rejects 1.0 for rev/version).
        return model.model_validate_json(data)
    except ValidationError:
        raise CourierFileError("invalid_schema") from None
