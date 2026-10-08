"""Minimal monotonic ULID generator (48-bit ms time + 80-bit randomness, Crockford base32)."""

from __future__ import annotations

import os
import re
import threading
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(ALPHABET)}
ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")  # \Z: `$` would also accept a trailing "\n"
_MAX_RAND = (1 << 80) - 1


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def _decode(s: str) -> int:
    v = 0
    for c in s:
        v = (v << 5) | _DECODE[c]
    return v


def is_ulid(s) -> bool:
    return isinstance(s, str) and bool(ULID_RE.match(s))


def ulid_ms(u: str) -> int:
    """Millisecond timestamp encoded in a ULID."""
    return _decode(u[:10])


def ulid_floor(ms: int) -> str:
    """Smallest ULID for a given millisecond; `ulid > floor` means 'at or after ms'."""
    return _encode(ms, 10) + "0" * 16


class ULIDGen:
    """Monotonic: same-ms IDs increment the random part; `seed()` carries that across restarts."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_rand = 0

    def seed(self, last: str):
        """Never issue an ID at or below `last` (the clock can come back behind)."""
        if not is_ulid(last):
            return
        with self._lock:
            ms, rand = ulid_ms(last), _decode(last[10:])
            if (ms, rand) > (self._last_ms, self._last_rand):
                self._last_ms, self._last_rand = ms, rand

    def new(self, now_ms: int | None = None) -> str:
        with self._lock:
            ms = int(time.time() * 1000) if now_ms is None else now_ms
            if ms <= self._last_ms:
                ms = self._last_ms
                rand = self._last_rand + 1
                if rand > _MAX_RAND:  # astronomically unlikely; borrow the next ms
                    ms += 1
                    rand = int.from_bytes(os.urandom(10), "big")
            else:
                rand = int.from_bytes(os.urandom(10), "big")
            self._last_ms, self._last_rand = ms, rand
            return _encode(ms, 10) + _encode(rand, 16)
