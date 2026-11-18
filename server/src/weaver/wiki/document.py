"""Lossless Markdown sections, visible properties, and code-first edit hunks."""

import hashlib
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

HUMAN_KEYS = frozenset(
    (
        "deadline",
        "schedule",
        "status",
        "assignees",
        "next",
        "area",
        "aliases",
        "tags",
        "cssclasses",
        "date",
    )
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
        if (
            not fence
            and key != MEMO
            and (
                line.startswith("## ") or re.fullmatch(r"##\s*내\s*메모\s*#*\s*", line)
            )
        ):
            if lines:
                result.append(Section(key, "".join(lines)))
            heading = (
                MEMO if re.fullmatch(r"##\s*내\s*메모\s*#*\s*", line) else line.strip()
            )
            counts[heading] = counts.get(heading, 0) + 1
            key = heading if counts[heading] == 1 else f"{heading} [{counts[heading]}]"
            lines = []
        lines.append(line)
    if lines:
        result.append(Section(key, "".join(lines)))
    return result


def restore_memo_heading(before: str, after: str) -> str:
    old_keys = {part.key for part in sections(before)}
    parts = sections(after)
    if MEMO not in old_keys or any(part.key == MEMO for part in parts):
        return after
    if parts and parts[-1].key not in old_keys and parts[-1].key != "__intro__":
        return (
            "".join(part.text for part in parts[:-1])
            + MEMO
            + "\n"
            + parts[-1].text.partition("\n")[2]
        )
    return after.rstrip() + "\n\n" + MEMO + "\n"


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

    lines, fence = [], ""
    for line in body.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            fence = (
                ""
                if fence and token[0] == fence[0] and len(token) >= len(fence)
                else token
                if not fence
                else fence
            )
            lines.append(line)
        else:
            lines.append(line if fence else re.sub(r"\[\[([^\]\n]+)\]\]", label, line))
    return "".join(lines)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def expression(text: str) -> str:
    # Keep punctuation, numbers, link destinations and checkbox states meaningful.
    text = re.sub(r"(?m)^ {0,3}(?:#{1,6}\s+|[-*+]\s+)", "", text)
    text = re.sub(r"(\*\*|__|`)", "", text)
    # A space can separate numbers/Latin identifiers. Korean layout whitespace
    # is cosmetic; quoted/struck text remains semantically significant.
    text = re.sub(r"(?<=[A-Za-z0-9])\s+(?=[A-Za-z0-9])", "␠", text)
    return re.sub(r"\s+", "", text)


def heading_moves(before: str, after: str) -> dict[str, str]:
    old = {s.key: s.text for s in sections(before)}
    new = {s.key: s.text for s in sections(after)}
    missing, added = set(old) - set(new), set(new) - set(old)
    result = {}
    for left in missing:
        candidates = [
            right
            for right in added
            if old[left].partition("\n")[2] == new[right].partition("\n")[2]
        ]
        if len(candidates) == 1:
            result[left] = candidates[0]
            added.remove(candidates[0])
    old_order = [part.key for part in sections(before)]
    new_order = [part.key for part in sections(after)]
    if len(old_order) == len(new_order):
        for left, right in zip(old_order, new_order, strict=True):
            if (
                left in missing
                and left not in result
                and right in added
                and left not in (MEMO, "## 근거")
            ):
                result[left] = right
                added.remove(right)
    return result


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
    original = {s.key: s.text for s in sections(before)}
    new = {s.key: s.text for s in sections(after)}
    result: list[Hunk] = []
    renamed = heading_moves(before, after)
    old = {}
    for key, text in original.items():
        target = renamed.get(key, key)
        if target != key:
            old_heading = text.partition("\n")[0]
            new_heading = new[target].partition("\n")[0]
            result.append(
                Hunk(len(result), target, old_heading, new_heading, "structure")
            )
            text = new_heading + "\n" + text.partition("\n")[2]
        old[target] = text
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
            Hunk(len(result), "__order__", "\n".join(old), "\n".join(new), "structure")
        )
    return result
