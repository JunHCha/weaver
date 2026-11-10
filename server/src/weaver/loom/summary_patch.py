"""Apply explicitly targeted edits without regenerating the document."""

from pydantic import BaseModel, ConfigDict


class Patch(BaseModel):
    """Replace one unambiguous substring in the original draft."""

    model_config = ConfigDict(extra="forbid")
    before: str
    after: str


def apply_patches(body: str, patches: list[Patch]) -> str:
    """Apply nonoverlapping replacements, rejecting ambiguous or missing targets."""
    spans: list[tuple[int, int, str]] = []
    for patch in patches:
        start = body.find(patch.before) if patch.before else -1
        if start < 0 or body.find(patch.before, start + 1) != -1:
            raise ValueError("patch target must occur exactly once")
        spans.append((start, start + len(patch.before), patch.after))
    spans.sort()
    for previous, current in zip(spans, spans[1:], strict=False):
        if previous[1] > current[0]:
            raise ValueError("patch targets overlap")
    result = body
    for start, end, replacement in reversed(spans):
        result = result[:start] + replacement + result[end:]
    if not result.strip():
        raise ValueError("patch result must not be empty")
    return result
