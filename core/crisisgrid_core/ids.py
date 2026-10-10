from __future__ import annotations

import os
import time
import uuid

__all__ = ["uuid7", "new_id"]


def uuid7() -> str:
    """RFC 9562 UUIDv7 string (time-ordered). Python 3.12 has no uuid.uuid7."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= ((rand >> 62) & 0xFFF) << 64  # rand_a (12 bits)
    value |= 0b10 << 62  # variant
    value |= rand & ((1 << 62) - 1)  # rand_b (62 bits)
    return str(uuid.UUID(int=value))


new_id = uuid7
