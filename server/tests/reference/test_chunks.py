from pathlib import PurePosixPath

from weaver.reference.chunks import indexable, parse

NOTE = (
    "---\ntags: [infra/network/router]\n---\n# R\n소개\n## 설정\n"
    "#stacks/fortigate 본문 [[infra/network/switch|스위치]]\n"
    "```\n## 코드 안 제목\n```\n"
)


def test_split_by_headings_with_frontmatter_tags_links() -> None:
    doc = parse("n.md", NOTE.encode())
    assert doc is not None
    assert doc.tags == ("infra/network/router", "stacks/fortigate")
    assert doc.links == ("infra/network/switch",)
    assert [c.heading for c in doc.chunks] == ["# R", "## 설정"]
    assert [c.ordinal for c in doc.chunks] == [0, 1]
    assert "## 코드 안 제목" in doc.chunks[1].text


def test_indexable_excludes() -> None:
    def ok(path: str) -> bool:
        return indexable(PurePosixPath(path), wiki_folder="Weaver")

    for path in (
        "a.pem",
        ".obsidian/x.md",
        "Weaver/p.md",
        "img.png",
        "n.sync-conflict-1.md",
        "x/.git/a.md",
        "3. Resource/drawing.excalidraw.md",
    ):
        assert not ok(path)
    assert ok("3. Resource/golf/a.md")
    assert ok("3. Resource/drawing.md")
    assert indexable(PurePosixPath("Weaver/p.md"), wiki_folder=None)


def test_binary_and_bad_frontmatter() -> None:
    assert parse("a.md", b"a\x00b") is None
    assert parse("a.md", b"\xff\xfe") is None
    doc = parse("a.md", b"---\na:\n  b: 1\n---\n# T\nbody\n")
    assert doc is not None
    assert doc.frontmatter == {}
    assert [c.heading for c in doc.chunks] == ["# T"]


def test_masks_secrets_and_splits_long_sections() -> None:
    body = "\n\n".join(f"문단 {i} " + "가" * 40 for i in range(10))
    doc = parse("a.md", f"# T\npassword: hunter2\n\n{body}\n".encode(), max_chunk=200)
    assert doc is not None
    assert len(doc.chunks) > 1
    assert all(c.heading == "# T" for c in doc.chunks)
    assert all(len(c.text) <= 200 for c in doc.chunks)
    assert "hunter2" not in "".join(c.text for c in doc.chunks)
    assert [c.ordinal for c in doc.chunks] == list(range(len(doc.chunks)))


def test_preamble_and_tag_forms() -> None:
    doc = parse("a.md", "intro #t/x 앞\n# H\n[[a#sec]] 코드 `#no`\n".encode())
    assert doc is not None
    assert doc.chunks[0].heading == ""
    assert doc.tags == ("t/x",)
    assert doc.links == ("a",)
    block = parse("a.md", b"---\ntags:\n  - a/b\n  - c\n---\nx\n")
    assert block is not None and block.tags == ("a/b", "c")


def test_links_and_tags_come_from_unmasked_text() -> None:
    slug = "infra/network/switch-01-core-distribution-layer"
    doc = parse(
        "a.md",
        f"# H\n[[{slug}]] #proj/2024-11-23-vendor-meeting-notes-fortigate\n".encode(),
    )
    assert doc is not None
    assert doc.links == (slug,)
    assert doc.tags == ("proj/2024-11-23-vendor-meeting-notes-fortigate",)
    assert slug in doc.chunks[0].text


def test_leading_rule_is_not_frontmatter() -> None:
    doc = parse("a.md", "---\n그냥 문단\n---\n# T\n본문\n".encode())
    assert doc is not None
    assert doc.chunks[0].text.startswith("---\n그냥 문단")


def test_frontmatter_accepts_indented_continuation() -> None:
    doc = parse("a.md", b"---\ntitle: x\n  more text\n---\n# T\nbody\n")
    assert doc is not None
    assert [c.heading for c in doc.chunks] == ["# T"]
    assert "title: x" not in doc.chunks[0].text
