"""Optional file adapter for the durable pipeline journal."""

import json
from pathlib import Path

from weaver.courier.atomic import write_bytes_atomic
from weaver.schema import PipelineItem


class PipelineProjection:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def publish(self, item: PipelineItem) -> None:
        if self.directory.is_symlink():
            raise OSError("pipeline projection directory is a symlink")
        self.directory.mkdir(parents=True, exist_ok=True)
        final = self.directory / f"{item.source_id}.json"
        payload = item.model_dump(mode="json")
        if not item.capture_hosts or set(item.capture_hosts) == {"unknown"}:
            payload.pop("capture_hosts", None)
        # Omitted card keys mean their defaults, so older readers never see them.
        if not item.summary:
            payload.pop("summary", None)
        if item.counterpart is None:
            payload.pop("counterpart", None)
        if not item.related_tags:
            payload.pop("related_tags", None)
        if item.wiki_edit is None:
            # Omit the Wiki-only extension for ordinary capture observations.
            payload.pop("wiki_edit", None)
        data = json.dumps(payload, ensure_ascii=False).encode()
        if final.is_symlink() or not final.is_file() or final.read_bytes() != data:
            write_bytes_atomic(final, data)
