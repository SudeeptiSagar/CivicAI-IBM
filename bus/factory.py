"""Bus selection.

Agents ask for a `Bus` and never name an implementation, so switching the
transport is a config change (PRD section 9.1).
"""

from __future__ import annotations

from bus.base import Bus
from bus.memory import InMemoryBus
from common.config import BusBackend, settings

__all__ = ["make_bus"]


def make_bus(backend: BusBackend | None = None) -> Bus:
    """Build the configured bus.

    Args:
        backend: override for `CIVICAI_BUS_BACKEND`. Tests pass "memory".
    """
    chosen = backend or settings().bus_backend

    if chosen == "memory":
        return InMemoryBus()

    if chosen == "redis":
        # Imported lazily so the in-memory path never requires the redis client.
        from bus.redis import RedisBus

        return RedisBus()

    raise ValueError(f"unknown bus backend: {chosen!r}")
