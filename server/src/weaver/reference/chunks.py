"""Split reference Markdown into heading chunks with tags and links."""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import JsonValue

from weaver.llm.policy import mask_secrets
from weaver.wiki.store import _properties

INDEXED_SUFFIX = ".md"
SECRET_SUFFIXES = frozenset(
    {".pem", ".pfx", ".p12", ".key", ".crt", ".cer", ".der", ".jks", ".keystore"}
)
EXCLUDED_DIRS = frozenset({".git", ".obsidian", ".trash", ".stversions"})

_HEADING = re.compile(r"#{1,3}[ \t]+\S")
_FENCE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_BODY_TAG = re.compile(r"(?<!\S)#([\w/-]*[^\W\d][\w/-]*)")
_FRONT_LINE = re.compile(r"(?:[ \t]+\S|[ \t]*(?:#|-(?:\s|$)|[^\s#-][^:]*:(?:\s|$)))")
_LINK = re.compile(r"\[\[([^\]|#]+)[^\]]*\]\]")


@dataclass(frozen=True)
class Chunk:
    heading: str
    ordinal: int
    text: str


@dataclass(frozen=True)
class Document:
    """``frontmatter`` is unmasked: callers must ``mask_secrets`` its values
    before persisting them."""

    path: str
    frontmatter: dict[str, JsonValue]
    tags: tuple[str, ...]
    links: tuple[str, ...]
    chunks: tuple[Chunk, ...]


def indexable(rel: PurePosixPath, *, wiki_folder: str | None) -> bool:
    if rel.suffix != INDEXED_SUFFIX or ".sync-conflict-" in rel.name:
        return False
    if rel.name.endswith(".excalidraw.md"):
        return False
    parents = rel.parts[:-1]
    if EXCLUDED_DIRS.intersection(parents):
        return False
    return not (wiki_folder and parents[:1] == (wiki_folder,))


def _split_frontmatter(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    if lines and lines[0] == "---":
        for index, line in enumerate(lines[1:], 1):
            if line == "---":
                block = lines[1:index]
                if _looks_like_frontmatter(block):
                    return block, "\n".join(lines[index + 1 :])
                break
    return [], text


def _looks_like_frontmatter(block: list[str]) -> bool:
    content = [line for line in block if line.strip()]
    return bool(content) and all(_FRONT_LINE.match(line) for line in content)


def _flat_tags(lines: list[str]) -> list[str]:
    """Read `tags:` leniently; unquoted flow lists are common in vaults."""
    tags: list[str] = []
    collecting = False
    for line in lines:
        head = re.match(r"tags[ \t]*:(.*)$", line)
        if head:
            collecting = True
            rest = head.group(1).strip().strip("[]")
            tags += [t for t in re.split(r"[,\s]+", rest) if t]
        elif collecting and (item := re.match(r"[ \t]*-[ \t]+(.+)$", line)):
            tags.append(item.group(1).strip())
        elif collecting and line.strip():
            collecting = False
    return [t.strip("\"'").lstrip("#") for t in tags]


def _frontmatter_tags(props: dict[str, JsonValue], lines: list[str]) -> list[str]:
    value = props.get("tags")
    if isinstance(value, str):
        return [t for t in re.split(r"[,\s]+", value) if t]
    if isinstance(value, list):
        return [t for t in value if isinstance(t, str)]
    return _flat_tags(lines)


def _sections(body: str) -> tuple[list[tuple[str, list[str]]], str]:
    """Return (heading, lines) sections and the body with code removed."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    prose: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        marker = _FENCE.match(line)
        if fence is not None:
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
            ):
                fence = None
            sections[-1][1].append(line)
            continue
        if marker:
            fence = marker.group(1)
            sections[-1][1].append(line)
            continue
        if _HEADING.match(line):
            sections.append((line.strip(), [line]))
        else:
            sections[-1][1].append(line)
        prose.append(_INLINE_CODE.sub("", line))
    return sections, "\n".join(prose)


def _pieces(text: str, limit: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for paragraph in re.split(r"\n[ \t]*\n", text):
        while len(paragraph) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        joined = f"{current}\n\n{paragraph}" if current else paragraph
        if len(joined) > limit:
            pieces.append(current)
            current = paragraph
        else:
            current = joined
    if current:
        pieces.append(current)
    return pieces


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in values if v))


def parse(path: str, data: bytes, *, max_chunk: int = 2000) -> Document | None:
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        props = _properties(data)
    except ValueError:
        props = {}
    front_lines, body = _split_frontmatter(text)
    _, prose = _sections(body)
    tags = _frontmatter_tags(props, front_lines) + _BODY_TAG.findall(prose)
    links = [m.strip() for m in _LINK.findall(prose)]
    chunks: list[Chunk] = []
    # Tags and links come from the original text; only chunk text is masked.
    sections, _ = _sections(mask_secrets(body))
    for heading, lines in sections:
        section = "\n".join(lines).strip()
        if not section:
            continue
        for piece in _pieces(section, max_chunk):
            chunks.append(Chunk(heading, len(chunks), mask_secrets(piece)))
    return Document(
        path,
        props,
        _unique([unicodedata.normalize("NFC", t.lstrip("#")) for t in tags]),
        _unique([unicodedata.normalize("NFC", link) for link in links]),
        tuple(chunks),
    )
