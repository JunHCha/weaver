"""Message-free crash diagnostics, durably stored outside the domain journal."""

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ApiError

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
Symbol = Annotated[
    str, Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_.$<>\[\]/-]+$")
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StackFrame(StrictModel):
    class_name: Symbol
    method_name: Symbol
    file_name: (
        Annotated[str, Field(max_length=256, pattern=r"^[A-Za-z0-9_.$<>-]+$")] | None
    ) = None
    line_number: int


class CrashException(StrictModel):
    exception_class: Symbol
    frames: list[StackFrame]


class ScreenPath(StrictModel):
    last_tab: Annotated[str, Field(pattern=r"^(NOW|AREAS|LOG|PEOPLE|WIKI)$")]
    map_open: bool
    selected_node: Annotated[str, Field(max_length=64, pattern=r"^[A-Z_]+$")] | None = (
        None
    )
    settings_open: bool = False


class CrashReport(StrictModel):
    client_id: Identifier
    occurred_at: datetime
    app_version: Annotated[str, Field(min_length=1, max_length=64)]
    app_build: int
    thread: Annotated[str, Field(min_length=1, max_length=128)]
    exceptions: Annotated[list[CrashException], Field(min_length=1)]
    screen: ScreenPath


class CrashStore:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root / "mobile" / "crashes"

    def submit(self, raw: bytes, device_id: str) -> tuple[int, dict[str, str]]:
        try:
            report = CrashReport.model_validate_json(raw)
            if report.occurred_at.tzinfo is None:
                raise ValueError("timezone required")
        except (ValidationError, ValueError):
            raise ApiError(422, "invalid_crash") from None
        report.occurred_at = report.occurred_at.astimezone(UTC)
        payload = report.model_dump(mode="json")
        record = {"device_id": device_id, "report": payload}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.root / f"{report.client_id}.json"
        fd, name = tempfile.mkstemp(prefix=".crash-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, target)  # atomic publish, never overwrite an existing ID
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                status = 201
            except FileExistsError:
                if self.get(report.client_id)["report"] != payload:
                    raise ApiError(409, "client_id_conflict") from None
                status = 200
        finally:
            Path(name).unlink(missing_ok=True)
        logging.getLogger("weaver.mobile").info("crash %s", report.client_id)
        return status, {"client_id": report.client_id, "state": "stored"}

    def get(self, client_id: str) -> dict[str, object]:
        import re

        if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", client_id) is None:
            raise ApiError(404, "crash_not_found")
        try:
            result: dict[str, object] = json.loads(
                (self.root / f"{client_id}.json").read_text()
            )
            return result
        except FileNotFoundError:
            raise ApiError(404, "crash_not_found") from None

    def records(self) -> list[dict[str, object]]:
        return [self.get(path.stem) for path in sorted(self.root.glob("*.json"))]
