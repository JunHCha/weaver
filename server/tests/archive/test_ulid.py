import re

import pytest

from weaver.archive.ulid import UlidGenerator, new_source_id
from weaver.schema.models import ULID


def test_source_id_format():
    assert re.fullmatch(rf"src_{ULID}", new_source_id())


def test_monotonic_within_one_millisecond():
    gen = UlidGenerator(clock_ms=lambda: 1_700_000_000_000)
    ids = [gen() for _ in range(1000)]
    assert ids == sorted(ids) and len(set(ids)) == 1000


def test_time_prefix_orders_ids():
    times = iter([1, 2])
    gen = UlidGenerator(clock_ms=lambda: next(times), randbytes=lambda n: b"\xff" * n)
    first, second = gen(), gen()
    assert first < second


def test_clock_going_backwards_stays_monotonic():
    times = iter([5_000, 4_000])
    gen = UlidGenerator(clock_ms=lambda: next(times))
    first, second = gen(), gen()
    assert first < second


def test_random_overflow_is_refused():
    gen = UlidGenerator(clock_ms=lambda: 7, randbytes=lambda n: b"\xff" * n)
    gen()
    with pytest.raises(OverflowError):
        gen()
