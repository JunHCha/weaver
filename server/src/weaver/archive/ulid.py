"""Monotonic ULIDs from the standard library (canonical uppercase Crockford)."""

import os
import threading
import time
from collections.abc import Callable

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_MAX_TIME = (1 << 48) - 1
_MAX_RANDOM = (1 << _RANDOM_BITS) - 1


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


class UlidGenerator:
    """48-bit millisecond time + 80 random bits, strictly increasing per process.

    Within one millisecond, or if the clock steps backwards, the previous random
    part is incremented, so IDs still sort in generation order.
    """

    def __init__(
        self,
        clock_ms: Callable[[], int] | None = None,
        randbytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._randbytes = randbytes
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_random = 0

    def __call__(self) -> str:
        with self._lock:
            ms = self._clock_ms()
            if not 0 <= ms <= _MAX_TIME:
                raise OverflowError("ULID time out of range")
            if ms <= self._last_ms:
                ms = self._last_ms
                random = self._last_random + 1
                if random > _MAX_RANDOM:
                    raise OverflowError("ULID random part exhausted")
            else:
                random = int.from_bytes(self._randbytes(10), "big")
            self._last_ms, self._last_random = ms, random
            return _encode((ms << _RANDOM_BITS) | random)


_default = UlidGenerator()


def new_source_id() -> str:
    return f"src_{_default()}"
