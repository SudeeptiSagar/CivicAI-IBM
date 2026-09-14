"""Identifier minting.

The PRD (section 9.2) specifies UUIDv7 for message_id, trace_id and causation_id.
Python only gained uuid.uuid7() in 3.14; this project pins 3.13, so v7 is
implemented here per RFC 9562 section 5.7.

Layout (128 bits, most significant first):

    48 bits  unix_ts_ms
     4 bits  version (0b0111)
    12 bits  rand_a
     2 bits  variant (0b10)
    62 bits  rand_b

Time ordering holds across milliseconds. Within a single millisecond the order
is random, which is fine: the PRD only guarantees ordering per correlation_id
(section 9.4), and that ordering comes from the bus, not from the id.
"""

from __future__ import annotations

import secrets
import time
import uuid

__all__ = ["new_message_id", "new_trace_id", "timestamp_ms_of", "uuid7"]

_VERSION_7 = 0x7
_VARIANT_RFC4122 = 0b10


def uuid7() -> uuid.UUID:
    """Mint a time-ordered UUIDv7."""
    unix_ts_ms = time.time_ns() // 1_000_000

    value = (unix_ts_ms & 0xFFFF_FFFF_FFFF) << 80
    value |= _VERSION_7 << 76
    value |= secrets.randbits(12) << 64
    value |= _VARIANT_RFC4122 << 62
    value |= secrets.randbits(62)

    return uuid.UUID(int=value)


def new_message_id() -> uuid.UUID:
    """Mint a message_id. One per message, never reused."""
    return uuid7()


def new_trace_id() -> uuid.UUID:
    """Mint a trace_id.

    Only A0 calls this. Every downstream message copies the trace_id unchanged
    so any incident can be replayed back to the original citizen submission.
    """
    return uuid7()


def timestamp_ms_of(value: uuid.UUID) -> int:
    """Extract the embedded millisecond timestamp from a UUIDv7.

    Raises:
        ValueError: if `value` is not a version 7 UUID.
    """
    if value.version != 7:
        raise ValueError(f"expected a UUIDv7, got version {value.version}")
    return value.int >> 80
