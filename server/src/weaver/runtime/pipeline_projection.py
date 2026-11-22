"""Optional file adapter for the durable pipeline journal."""

import json
from pathlib import Path

from weaver.courier.atomic import write_bytes_atomic
from weaver.schema import PipelineItem


def pipeline_payload(item: PipelineItem) -> dict[str, object]:
    """Share the v2 default-key omission contract across file and HTTP adapters."""
    payload = item.model_dump(mode="json")
    if not item.capture_hosts or set(item.capture_hosts) == {"unknown"}:
        payload.pop("capture_hosts", None)
    for key, empty in (
        ("summary", not item.summary),
        ("counterpart", item.counterpart is None),
        ("related_tags", not item.related_tags),
        ("wiki_edit", item.wiki_edit is None),
    ):
        if empty:
            payload.pop(key, None)
    return payload


class PipelineProjection:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def publish(self, item: PipelineItem) -> None:
        if self.directory.is_symlink():
            raise OSError("pipeline projection directory is a symlink")
        self.directory.mkdir(parents=True, exist_ok=True)
        final = self.directory / f"{item.source_id}.json"
        payload = pipeline_payload(item)
        data = json.dumps(payload, ensure_ascii=False).encode()
        if final.is_symlink() or not final.is_file() or final.read_bytes() != data:
            write_bytes_atomic(final, data)
