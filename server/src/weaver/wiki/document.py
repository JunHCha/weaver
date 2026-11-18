"""Lossless Markdown sections, visible properties, and code-first edit hunks."""

import hashlib
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

HUMAN_KEYS = frozenset(
    ("deadline", "schedule", "status", "assignees", "next", "area", "aliases")
)
MEMO = "## 내 메모"
NOTICE = "> 자동 생성 문서입니다. 수정한 내용은 다음 컴파일 때 되돌립니다.\n\n"


@dataclass(frozen=True)
class Section:
    key: str
    text: str


def sections(body: str) -> list[Section]:
    """Split on real H2 headings; fenced examples are not section boundaries."""
    result: list[Section] = []
    key, fence = "__intro__", ""
    lines: list[str] = []
    counts: dict[str, int] = {}
    for line in body.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
        if not fence and key != MEMO and line.startswith("## "):
            if lines:
                result.append(Section(key, "".join(lines)))
            heading = line.strip()
            counts[heading] = counts.get(heading, 0) + 1
            key = heading if counts[heading] == 1 else f"{heading} [{counts[heading]}]"
            lines = []
        lines.append(line)
    if lines:
        result.append(Section(key, "".join(lines)))
    return result


def without_memo(body: str) -> str:
    return "".join(s.text for s in sections(body) if s.key != MEMO)


def with_memo(generated: str, previous: str) -> str:
    memo = next((s.text for s in sections(previous) if s.key == MEMO), MEMO + "\n")
    return without_memo(generated).rstrip("\n") + "\n\n" + memo


def readable_links(body: str) -> str:
    def label(match: re.Match[str]) -> str:
        target = match[1]
        if "|" in target:
            return match[0]
        name = target.rsplit("/", 1)[-1].removesuffix(".md")
        name = re.sub(r" src_[A-Za-z0-9]+$", "", name)
        return f"[[{target}|{name}]]"

    return re.sub(r"\[\[([^\]\n]+)\]\]", label, body)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def expression(text: str) -> str:
    # Keep punctuation, numbers, link destinations and checkbox states meaningful.
    text = re.sub(r"(?m)^ {0,3}(?:#{1,6}\s+|[-*+]\s+|>\s*)", "", text)
    text = re.sub(r"(\*\*|__|`|~~)", "", text)
    return re.sub(r"\s+", "", text)


@dataclass(frozen=True)
class Hunk:
    id: int
    section: str
    before: str
    after: str
    classification: str

    def payload(self) -> dict[str, object]:
        return asdict(self)


def hunks(before: str, after: str) -> list[Hunk]:
    """Diff lines within sections so adjacent memo/body changes never mix."""
    old = {s.key: s.text for s in sections(before)}
    new = {s.key: s.text for s in sections(after)}
    result: list[Hunk] = []
    for key in dict.fromkeys([*old, *new]):
        a, b = (
            old.get(key, "").splitlines(keepends=True),
            new.get(key, "").splitlines(keepends=True),
        )
        for group in SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(0):
            left = "".join(a[group[0][1] : group[-1][2]])
            right = "".join(b[group[0][3] : group[-1][4]])
            kind = (
                "memo"
                if key == MEMO
                else (
                    "metadata"
                    if key == "## 근거"
                    else (
                        "expression"
                        if expression(left) == expression(right)
                        else "content"
                    )
                )
            )
            result.append(Hunk(len(result), key, left, right, kind))
    if list(old) != list(new) and set(old) == set(new):
        result.append(
            Hunk(len(result), "__order__", "\n".join(old), "\n".join(new), "content")
        )
    return result
