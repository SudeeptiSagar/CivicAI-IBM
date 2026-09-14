"""Transport abstraction (PRD section 9.1).

Agents talk to the bus only through this interface, so Redis Streams can be
swapped for Kafka or RabbitMQ without touching agent code. The shape follows
Redis Streams deliberately — consumer groups, explicit ack, reclaimable pending
entries — because modelling the weaker guarantee is what keeps handlers honest.

Guarantees the interface promises (PRD section 9.4):

* at-least-once delivery; handlers must be idempotent (see common.idempotency)
* ordering per `correlation_id` only, never global
* a message is redelivered until acked, so a crash mid-handler loses nothing
* `delivery_count` lets a consumer spot a poison pill and quarantine it rather
  than re-queueing it forever
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

__all__ = ["MAX_DELIVERIES", "Bus", "BusMessage"]

#: Attempts before a message is treated as a poison pill (PRD section 9.4:
#: three attempts, then deadletter).
MAX_DELIVERIES = 3


@dataclass(frozen=True, slots=True)
class BusMessage:
    """One delivery of one message.

    `message_id` is the bus's id for the stream entry, which is not the
    envelope's `message_id`. The envelope keeps its own identity; this one
    exists so `ack`/`nack` can address a specific delivery.
    """

    message_id: str
    topic: str
    envelope: dict[str, Any] = field(repr=False)
    delivery_count: int = 1

    @property
    def is_poison(self) -> bool:
        """True once this delivery has exhausted the retry budget."""
        return self.delivery_count >= MAX_DELIVERIES


class Bus(abc.ABC):
    """Publish/subscribe transport with consumer groups."""

    @abc.abstractmethod
    def publish(self, topic: str, envelope: dict[str, Any]) -> str:
        """Append `envelope` to `topic`. Returns the bus message id."""

    @abc.abstractmethod
    def create_group(self, topic: str, group: str) -> None:
        """Ensure a consumer group exists on `topic`. Idempotent.

        A group created now starts at the tail: it sees messages published
        after creation, not the backlog. Replay is an explicit operation, not
        a side effect of subscribing.
        """

    @abc.abstractmethod
    def subscribe(
        self,
        topic: str,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 0,
    ) -> list[BusMessage]:
        """Claim up to `count` undelivered messages for `consumer`.

        Claimed messages stay pending until acked or nacked. `block_ms` is how
        long to wait for a message when none is ready; 0 returns immediately.
        """

    @abc.abstractmethod
    def ack(self, topic: str, group: str, message_id: str) -> None:
        """Mark a delivery handled. Removes it from the pending list."""

    @abc.abstractmethod
    def nack(self, topic: str, group: str, message_id: str) -> None:
        """Return a delivery for redelivery, incrementing its delivery count."""

    @abc.abstractmethod
    def pending(self, topic: str, group: str) -> list[BusMessage]:
        """Deliveries claimed but not yet acked, oldest first."""

    @abc.abstractmethod
    def reclaim(
        self, topic: str, group: str, consumer: str, *, min_idle_ms: int
    ) -> list[BusMessage]:
        """Take over deliveries stalled on another consumer.

        The Redis Streams XAUTOCLAIM behaviour the PRD calls for: a consumer
        that dies mid-handler must not strand its claimed messages.
        """

    @abc.abstractmethod
    def length(self, topic: str) -> int:
        """Number of messages on `topic`. Diagnostics and tests."""
