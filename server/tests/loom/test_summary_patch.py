"""Feedback must preserve all document text outside explicitly addressed patches."""

import pytest

from weaver.loom.summary_patch import Patch, apply_patches


def test_patches_preserve_unrelated_content_and_original_offsets() -> None:
    original = "첫 문장\n둘째 문장\n셋째 문장"
    result = apply_patches(
        original,
        [
            Patch(before="첫 문장", after="새 첫 문장"),
            Patch(before="셋째 문장", after="끝"),
        ],
    )
    assert result == "새 첫 문장\n둘째 문장\n끝"


@pytest.mark.parametrize(
    "patches",
    [
        [Patch(before="없는 내용", after="변경")],
        [Patch(before="문장", after="변경")],
        [
            Patch(before="첫 문장", after="변경"),
            Patch(before="첫 문장", after="다른 변경"),
        ],
        [Patch(before="", after="추가")],
    ],
)
def test_rejects_ambiguous_missing_and_overlapping_patches(
    patches: list[Patch],
) -> None:
    with pytest.raises(ValueError):
        apply_patches("첫 문장\n둘째 문장", patches)


def test_overlapping_occurrences_are_ambiguous() -> None:
    with pytest.raises(ValueError):
        apply_patches("aaa", [Patch(before="aa", after="b")])
