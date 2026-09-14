"""In-memory bus (PRD section 11: "redis impl + in-memory test impl").

Reproduces the Redis Streams semantics the agents are written against —
consumer groups, explicit ack, redelivery on nack, reclaimable stalled
deliveries — without requiring Redis. Used by the test suite and by CI, where
no infrastructure is available.

This is a faithful test double, not a production transport: everything lives in
one process and is lost on exit. `bus.redis.RedisBus` (P1) is the real one.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from bus.base import Bus, BusMessage

__all__ = ["InMemoryBus"]


@dataclass(slots=True)
class _Entry:
    """One published message."""

    message_id: str
    topic: str
    envelope: dict[str, Any]


@dataclass(slots=True)
class _Delivery:
    """A claimed-but-unacked message."""

    entry: _Entry
    consumer: str
    delivery_count: int
    claimed_at: float


@dataclass(slots=True)
class _Group:
    """A consumer group's position and in-flight work."""

    cursor: int
    pending: dict[str, _Delivery] = field(default_factory=dict)
    retry: deque[str] = field(default_factory=deque)
    delivery_counts: dict[str, int] = field(default_factory=dict)


class InMemoryBus(Bus):
    """Thread-safe in-process implementation of `Bus`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._published = threading.Condition(self._lock)
        self._streams: dict[str, list[_Entry]] = {}
        self._entries: dict[str, _Entry] = {}
        self._groups: dict[tuple[str, str], _Group] = {}
        self._seq = itertools.count()

    # -- producer side ----------------------------------------------------

    def publish(self, topic: str, envelope: dict[str, Any]) -> str:
        with self._published:
            message_id = f"{next(self._seq)}-0"
            entry = _Entry(message_id=message_id, topic=topic, envelope=dict(envelope))
            self._streams.setdefault(topic, []).append(entry)
            self._entries[message_id] = entry
            self._published.notify_all()
            return message_id

    # -- consumer side ----------------------------------------------------

    def create_group(self, topic: str, group: str) -> None:
        with self._lock:
            key = (topic, group)
            if key not in self._groups:
                # Start at the tail: subscribing must not replay the backlog.
                self._groups[key] = _Group(cursor=len(self._streams.get(topic, [])))

    def subscribe(
        self,
        topic: str,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 0,
    ) -> list[BusMessage]:
        deadline = time.monotonic() + block_ms / 1000.0

        with self._published:
            self.create_group(topic, group)
            while True:
                claimed = self._claim(topic, group, consumer, count)
                if claimed or block_ms == 0:
                    return claimed

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._published.wait(remaining)

    def ack(self, topic: str, group: str, message_id: str) -> None:
        with self._lock:
            g = self._group(topic, group)
            delivery = g.pending.pop(message_id, None)
            if delivery is None:
                raise KeyError(f"{message_id!r} is not pending for group {group!r} on {topic!r}")
            g.delivery_counts.pop(message_id, None)

    def nack(self, topic: str, group: str, message_id: str) -> None:
        with self._published:
            g = self._group(topic, group)
            delivery = g.pending.pop(message_id, None)
            if delivery is None:
                raise KeyError(f"{message_id!r} is not pending for group {group!r} on {topic!r}")
            g.delivery_counts[message_id] = delivery.delivery_count
            g.retry.append(message_id)
            self._published.notify_all()

    def pending(self, topic: str, group: str) -> list[BusMessage]:
        with self._lock:
            g = self._group(topic, group)
            ordered = sorted(g.pending.values(), key=lambda d: d.claimed_at)
            return [self._to_message(d) for d in ordered]

    def reclaim(
        self, topic: str, group: str, consumer: str, *, min_idle_ms: int
    ) -> list[BusMessage]:
        cutoff = time.monotonic() - min_idle_ms / 1000.0
        with self._lock:
            g = self._group(topic, group)
            reclaimed: list[BusMessage] = []
            for delivery in sorted(g.pending.values(), key=lambda d: d.claimed_at):
                if delivery.claimed_at > cutoff:
                    continue
                delivery.consumer = consumer
                delivery.delivery_count += 1
                delivery.claimed_at = time.monotonic()
                reclaimed.append(self._to_message(delivery))
            return reclaimed

    def length(self, topic: str) -> int:
        with self._lock:
            return len(self._streams.get(topic, []))

    # -- test helpers -----------------------------------------------------

    def messages(self, topic: str) -> list[dict[str, Any]]:
        """Every envelope published to `topic`, in order. Tests only."""
        with self._lock:
            return [entry.envelope for entry in self._streams.get(topic, [])]

    def topics(self) -> list[str]:
        """Topics that have received at least one message. Tests only."""
        with self._lock:
            return sorted(self._streams)

    def clear(self) -> None:
        """Drop all streams and groups. Tests only."""
        with self._lock:
            self._streams.clear()
            self._entries.clear()
            self._groups.clear()

    # -- internals --------------------------------------------------------

    def _group(self, topic: str, group: str) -> _Group:
        try:
            return self._groups[(topic, group)]
        except KeyError:
            raise KeyError(f"no consumer group {group!r} on topic {topic!r}") from None

    def _claim(self, topic: str, group: str, consumer: str, count: int) -> list[BusMessage]:
        """Claim redeliveries first, then new messages. Caller holds the lock."""
        g = self._group(topic, group)
        stream = self._streams.setdefault(topic, [])
        claimed: list[BusMessage] = []

        while g.retry and len(claimed) < count:
            message_id = g.retry.popleft()
            entry = self._entries[message_id]
            claimed.append(
                self._track(g, entry, consumer, g.delivery_counts.get(message_id, 1) + 1)
            )

        while g.cursor < len(stream) and len(claimed) < count:
            entry = stream[g.cursor]
            g.cursor += 1
            claimed.append(self._track(g, entry, consumer, 1))

        return claimed

    def _track(self, g: _Group, entry: _Entry, consumer: str, delivery_count: int) -> BusMessage:
        delivery = _Delivery(
            entry=entry,
            consumer=consumer,
            delivery_count=delivery_count,
            claimed_at=time.monotonic(),
        )
        g.pending[entry.message_id] = delivery
        return self._to_message(delivery)

    @staticmethod
    def _to_message(delivery: _Delivery) -> BusMessage:
        return BusMessage(
            message_id=delivery.entry.message_id,
            topic=delivery.entry.topic,
            envelope=delivery.entry.envelope,
            delivery_count=delivery.delivery_count,
        )
