"""Tree nodes for vault tags and folders; kinds follow the namespace."""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from weaver.context import Kind

UNCLASSIFIED = "미분류"
_COMPANY_NAMESPACES = {"infra", "domain", "stacks", "project"}
_NUMBERING = re.compile(r"^\d+\.\s*")
_PROJECT = {"project", "projects"}
_AREA_OR_RESOURCE = {"area", "areas", "resource", "resources"}
_ARCHIVE = {"archive", "archives"}


@dataclass(frozen=True)
class TreeNode:
    name: str
    kind: Kind


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def root_of(path: str) -> str:
    parts = _parts(path)
    return parts[0] if parts else ""


def node_kind(path: str) -> Kind:
    parts = _parts(path)
    depth = len(parts)
    if depth <= 1:
        return "area"
    second = parts[1]
    if second == UNCLASSIFIED:
        return "area"
    if second == "project" and depth >= 3:
        return "project"
    if second in _COMPANY_NAMESPACES:
        if depth == 2:
            return "topic"
        if second in {"infra", "domain"} and depth == 3:
            return "area"
        return "topic"
    return "area" if depth == 2 else "topic"


def area_of(path: str) -> str | None:
    parts = _parts(path)
    if len(parts) < 2:
        return None
    second = parts[1]
    if second == UNCLASSIFIED:
        return f"{parts[0]}/{UNCLASSIFIED}"
    if second in {"project", "stacks"}:
        return None
    if second in {"infra", "domain"}:
        return "/".join(parts[:3]) if len(parts) >= 3 else None
    return "/".join(parts[:2])


def area_label(path: str) -> str:
    parts = _parts(path)
    last = parts[-1] if parts else ""
    if last.isascii() and last:
        return last[0].upper() + last[1:]
    return last


def home_path(sphere: str, folder: str) -> str | None:
    """Map a home-vault PARA folder to a tree path; Archive is left out."""
    parts = [_NUMBERING.sub("", p) for p in _parts(folder)]
    if not parts:
        return sphere
    head = parts[0].casefold()
    rest = parts[1:]
    if head in _ARCHIVE:
        return None
    if head in _PROJECT:
        return "/".join([sphere, "project", *rest]) if rest else sphere
    if head in _AREA_OR_RESOURCE:
        return "/".join([sphere, *rest]) if rest else sphere
    return "/".join([sphere, *parts])


def expand(sphere: str, tags: Iterable[str]) -> list[TreeNode]:
    names = {sphere, f"{sphere}/{UNCLASSIFIED}"}
    for tag in tags:
        parts = _parts(tag)
        for end in range(1, len(parts) + 1):
            names.add("/".join(parts[:end]))
    return [TreeNode(name, node_kind(name)) for name in sorted(names)]
