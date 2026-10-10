from __future__ import annotations

import os
import threading
import time
import uuid

__all__ = ["uuid7", "new_id"]

_lock = threading.Lock()
_last_ms = 0
_seq = 0


def uuid7() -> str:
    """RFC 9562 UUIDv7 string. Python 3.12 has no uuid.uuid7.

    Strictly increasing within a process (12-bit counter in rand_a, RFC 9562 method 1), so string order equals
    creation order even for ids created in the same millisecond.
    """
    global _last_ms, _seq
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            ms = _last_ms
            _seq += 1
            if _seq > 0xFFF:  # counter exhausted within this ms: borrow the next one
                ms += 1
                _seq = 0
        else:
            _seq = 0
        _last_ms = ms
        seq = _seq
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= seq << 64  # rand_a
    value |= 0b10 << 62  # variant
    value |= rand_b
    return str(uuid.UUID(int=value))


new_id = uuid7
