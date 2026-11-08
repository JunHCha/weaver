"""Bounded, frontmatter-only reader for Publisher's flat property dialect.

Supports JSON/YAML quoted and plain scalars, inline JSON lists and YAML block
lists. Nested objects, aliases, tags, multiline values and duplicate keys are
rejected for the whole file, including unknown properties. Unknown flat properties
are validated but omitted from the result. No YAML engine or Markdown parser is used.
"""

import json
import re
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


def _without_comment(text: str) -> str:
    """Strip YAML comments while respecting doubled quotes and JSON escapes."""
    quote: str | None = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote is not None:
            if char == "\\" and quote == '"':
                i += 2
                continue
            if char == quote:
                if quote == "'" and text[i : i + 2] == "''":
                    i += 2
                    continue
                quote = None
        elif char == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i].strip()
        elif char in ("'", '"') and (
            i == 0 or text[i - 1].isspace() or text[i - 1] in "[,"
        ):
            quote = char
        i += 1
    if quote is not None:
        raise ValueError("unclosed quote")
    return text.strip()


def _scalar(text: str) -> JsonValue:
    text = _without_comment(text.strip())
    if not text or text in ("null", "Null", "NULL", "~"):
        return None
    if text.startswith("'"):
        if re.fullmatch(r"'(?:[^']|'')*'", text) is None:
            raise ValueError("unsupported quoted scalar")
        return text[1:-1].replace("''", "'")
    if text in ("true", "True", "TRUE", "false", "False", "FALSE"):
        return text.lower() == "true"
    if text.startswith(('"', "[")):
        value: JsonValue = json.loads(text)
        if isinstance(value, list) and any(
            isinstance(item, (dict, list)) for item in value
        ):
            raise ValueError("unsupported nested value")
        return value
    if (
        text.startswith(("{", "|", ">", "&", "*", "!", "- ", "? ", "@", "`", "%"))
        or text
        in (
            "-",
            "?",
            "yes",
            "Yes",
            "YES",
            "no",
            "No",
            "NO",
            "on",
            "On",
            "ON",
            "off",
            "Off",
            "OFF",
        )
        or re.search(r":(?:\s|$)", text)
        or any(char in text for char in "[]{}")
    ):
        raise ValueError("unsupported scalar")
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", text):
        return int(text)
    # Other YAML numeric spellings and implicit booleans are outside this
    # dialect. Reject them rather than turning a typed value into human text.
    if re.fullmatch(
        r"[-+]?(?:0[xob][0-9a-f_]+|[0-9][0-9_]*(?:\.[0-9_]*)?"
        r"(?::[0-9_]+(?:\.[0-9_]*)?)*(?:e[-+]?[0-9]+)?|"
        r"\.[0-9]+(?:e[-+]?[0-9]+)?|\.inf|\.nan)",
        text,
        re.IGNORECASE,
    ):
        raise ValueError("unsupported scalar type")
    return text


def read_properties(path: Path) -> dict[str, JsonValue]:
    """Read no body bytes through the parser, even for untracked notes."""
    with path.open("rb") as stream:
        if stream.readline(5).rstrip(b"\r\n") != b"---":
            return {}
        result: dict[str, JsonValue] = {}
        current: str | None = None
        properties: dict[str, JsonValue] = {}
        sequence_allowed = False
        sequence_indent: int | None = None
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
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            if stripped == "-" or stripped.startswith("- "):
                if current is None or not sequence_allowed:
                    raise ValueError("unexpected block sequence")
                if sequence_indent is not None and indent != sequence_indent:
                    raise ValueError("unsupported nested sequence")
                sequence_indent = indent
                value = properties[current]
                if value is None:
                    value = []
                    properties[current] = value
                assert isinstance(value, list)
                item = _scalar(stripped[1:])
                if isinstance(item, (dict, list)):
                    raise ValueError("unsupported nested sequence")
                value.append(item)
                if current in KEYS:
                    result[current] = value
                continue
            if indent or line[0].isspace():
                raise ValueError("unsupported property structure")
            key, sep, text = line.partition(":")
            if not sep or re.fullmatch(r"[A-Za-z_][\w-]*", key) is None:
                raise ValueError("unsupported property key")
            if key in properties:
                raise ValueError("duplicate property")
            current = key
            sequence_allowed = not _without_comment(text.strip())
            sequence_indent = None
            properties[key] = _scalar(text)
            if key in KEYS:
                result[key] = properties[key]
    raise ValueError("oversized frontmatter")
