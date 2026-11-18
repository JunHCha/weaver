"""Shared Obsidian-safe source paths and deterministic legacy link repair."""

import re
import unicodedata
from collections.abc import Mapping


def summary_path(title: str, source_id: str) -> str:
    """Keep source IDs unique while excluding filename and wikilink syntax."""
    title = unicodedata.normalize("NFC", title)
    safe = re.sub(r"[/\\#^|\[\]\x00-\x1f\x7f]", "-", title).strip()
    return f"summaries/{safe} {source_id}.md"


def link_label(title: str) -> str:
    """A display label must not close or split the enclosing wikilink."""
    return re.sub(r"[\[\]|\r\n]", " ", title).strip()


def wikilink(target: str | None, label: str | None = None) -> str:
    """Missing source notes must not become phantom Obsidian pages."""
    if not target:
        return ""
    target = unicodedata.normalize("NFC", target)
    if label is not None:
        return f"[[{target}|{link_label(label)}]]"
    if target.startswith("summaries/") and "|" not in target:
        label = re.sub(
            r" src_[A-Za-z0-9]+$", "", target.rsplit("/", 1)[-1].removesuffix(".md")
        )
        return f"[[{target}|{link_label(label)}]]"
    return f"[[{target}]]"


def rewrite_links(body: str, replacements: Mapping[str, str]) -> str:
    """Replace exact known legacy targets, keeping labels and unrelated text."""
    normalized = {
        unicodedata.normalize("NFC", old): new for old, new in replacements.items()
    }

    def rewrite(match: re.Match[str]) -> str:
        value = unicodedata.normalize("NFC", match[1])
        # A legacy title may itself contain |, so try the whole target first.
        if value in normalized:
            return wikilink(normalized[value])
        target, sep, label = value.partition("|")
        if target in normalized:
            return wikilink(normalized[target] + sep + label)
        return match[0]

    return re.sub(r"\[\[([^\]\n]+)\]\]", rewrite, body)
