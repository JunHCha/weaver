"""Bounded, frontmatter-only reader for Publisher's flat property dialect.

Supports JSON/YAML quoted and plain scalars, inline JSON lists and YAML block
lists. Nested objects, aliases, tags, multiline values and duplicate keys are
rejected for Weaver properties. No YAML engine or Markdown parser is needed.
Unknown properties are ignored, so ordinary Obsidian metadata is unrestricted.
"""

import json
from pathlib import Path

from pydantic import JsonValue

from .fields import OUTPUT_FIELDS

KEYS = {
    "weaver_schema_version",
    "weaver_note_id",
    "weaver_source_ids",
    "weaver_entity_ids",
    "weaver_entry_ids",
    *OUTPUT_FIELDS,
}
MAX_FRONTMATTER = 64 * 1024


def _scalar(text: str) -> JsonValue:
    text = text.strip()
    if not text or text in ("null", "Null", "NULL", "~"):
        return None
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    if text.startswith(('"', "[", "{")) or text in ("true", "false"):
        value: JsonValue = json.loads(text)
        return value
    if text in ("|", ">") or text.startswith(("&", "*", "!")):
        raise ValueError("unsupported scalar")
    try:
        return int(text)
    except ValueError:
        return text.split(" #", 1)[0].strip()


def read_properties(path: Path) -> dict[str, JsonValue]:
    """Read no body bytes through the parser, even for untracked notes."""
    with path.open("rb") as stream:
        if stream.readline(5).rstrip(b"\r\n") != b"---":
            return {}
        result: dict[str, JsonValue] = {}
        current: str | None = None
        used = 0
        while used <= MAX_FRONTMATTER:
            raw = stream.readline(MAX_FRONTMATTER + 1)
            used += len(raw)
            if not raw or used > MAX_FRONTMATTER:
                raise ValueError("unclosed or oversized frontmatter")
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "---":
                return result
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0].isspace():
                if current is not None:
                    value = result[current]
                    if value is None:
                        value = []
                        result[current] = value
                    if not line.lstrip().startswith("- ") or not isinstance(
                        value, list
                    ):
                        raise ValueError("unsupported property structure")
                    value.append(_scalar(line.lstrip()[2:]))
                continue
            key, sep, text = line.partition(":")
            current = key if sep and key in KEYS else None
            if current is None:
                continue
            if key in result:
                raise ValueError("duplicate property")
            result[key] = _scalar(text)
    raise ValueError("oversized frontmatter")
