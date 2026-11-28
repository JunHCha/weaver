"""Interpreting a call script never drops or moves a character.

For generated Galaxy-style scripts (any newline style, optional BOM, blank and
malformed lines), Segments are exactly the nonempty lines, in order, and the
original text is kept verbatim. Examples come from a seeded generator so any
failure is reproducible from the reported seed, example index and text.
"""

import random

from server.tests.loom.test_call_parser import assert_coverage, call_source

from weaver.loom.call import CallParseError, parse_call

SEED = 20261128
EXAMPLES = 300
LINES = [
    "AI로 생성된 콘텐츠입니다",
    "가짜 제목",
    "나  (00:01)",
    "상대  (00:03)",
    "나 & 상대  (1:00:07)",
    "나  (00:60)",
    "(",
    "본문 😀",
    "가짜 본문 ",
    "",
    " \t",
]


def generate(rng: random.Random) -> str:
    newline = rng.choice(["\n", "\r\n"])
    text = newline.join(rng.choice(LINES) for _ in range(rng.randint(1, 12)))
    if rng.random() < 0.5:
        text += newline
    return rng.choice(["", "\ufeff"]) + text


def test_call_script_text_and_line_coordinates_are_lossless():
    rng = random.Random(SEED)
    for index in range(EXAMPLES):
        text = generate(rng)
        try:
            result = parse_call(call_source(), text)
        except CallParseError:
            continue  # explicit safe error; nothing is published
        try:
            assert_coverage(result, text)
        except AssertionError as error:
            raise AssertionError(
                f"seed={SEED} example={index} text={text!r}"
            ) from error
