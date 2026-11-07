"""File names, ignore rules and bounded, fail-closed JSON reading."""

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from weaver.schema.models import ULID

MAX_JSON_BYTES = 1 << 20
CONFLICT_MARKER = ".sync-conflict-"

MANIFEST_NAME = re.compile(rf"^(cap_{ULID})\.json$")
REVISION_NAME = re.compile(rf"^(prp_{ULID})\.r([1-9][0-9]*)\.json$")
ALERT_NAME = re.compile(rf"^alert_(alt_{ULID})\.json$")


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


def read_regular_bytes(path: Path, limit: int | None = MAX_JSON_BYTES) -> bytes:
    """Read a regular non-symlink file, failing closed on anything else."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise CourierFileError("missing") from None
    except OSError:
        raise CourierFileError("unreadable") from None
    if stat.S_ISLNK(info.st_mode):
        raise CourierFileError("symlink")
    if not stat.S_ISREG(info.st_mode):
        raise CourierFileError("not_regular_file")
    try:
        with path.open("rb") as stream:
            data = stream.read() if limit is None else stream.read(limit + 1)
    except FileNotFoundError:
        raise CourierFileError("missing") from None
    except OSError:
        raise CourierFileError("unreadable") from None
    if limit is not None and len(data) > limit:
        raise CourierFileError("too_large")
    return data


def load_versioned[M: BaseModel](path: Path, model: type[M]) -> M:
    """Parse one wire file. Unknown versions are reported, never interpreted."""
    data = read_regular_bytes(path)
    try:
        raw: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CourierFileError("invalid_json") from None
    if not isinstance(raw, dict):
        raise CourierFileError("invalid_schema")
    version = raw.get("schema_version")
    if type(version) is int and version != 1:
        raise CourierFileError("unknown_version")
    try:
        # JSON mode keeps integer-token strictness (rejects 1.0 for rev/version).
        return model.model_validate_json(data)
    except ValidationError:
        raise CourierFileError("invalid_schema") from None
