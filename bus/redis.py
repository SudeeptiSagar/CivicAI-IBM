"""Redis Streams transport (PRD section 9.1).

One stream per topic, one consumer group per agent. At-least-once delivery,
consumer-side idempotency, XAUTOCLAIM for stalled consumers.

Redelivery works differently here than in the in-memory double, and the
difference is worth stating. Redis has no "return to queue" operation: an
un-acked entry simply stays in the group's pending entries list (PEL). So
`nack()` records nothing and does nothing destructive — it just declines to
ack — and the next `subscribe()` picks the entry back up by draining its own
PEL before reading new messages. The observable behaviour matches
`InMemoryBus`: nacked work comes back, and its delivery count climbs.

The PEL-first ordering matters. Reading new messages first would let a busy
topic starve a message that keeps failing, and it would never reach the retry
budget that turns it into a deadletter.
"""

from __future__ import annotations

import json
from functools import cached_property
from typing import Any, cast

from redis import Redis
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from bus.base import Bus, BusMessage
from common.config import settings

__all__ = ["RedisBus", "stream_key"]

#: Streams are namespaced so CivicAI can share a Redis with anything else.
KEY_PREFIX = "civicai:stream:"

#: The envelope is stored as a single JSON field rather than flattened into
#: stream fields: the envelope is already a versioned contract, and flattening
#: would create a second, undocumented encoding of it.
ENVELOPE_FIELD = "envelope"

# redis-py types its stream replies as broad unions covering both the bytes
# and the decoded case. This client sets decode_responses=True, so every
# reply is str; these aliases state that once rather than at each call site.
StreamEntry = tuple[str, dict[str, str]]
PendingEntry = dict[str, Any]


def stream_key(topic: str) -> str:
    """The Redis key holding `topic`'s stream."""
    return f"{KEY_PREFIX}{topic}"


class RedisBus(Bus):
    """`Bus` backed by Redis Streams."""

    def __init__(self, url: str | None = None, *, maxlen: int | None = 100_000) -> None:
        """
        Args:
            url: Redis connection string. Defaults to the configured one.
            maxlen: approximate cap on stream length. Postgres holds the durable
                archive (PRD section 6.1), so trimming the stream loses no truth.
                None disables trimming.
        """
        self._url = url or settings().redis_url
        self._maxlen = maxlen

    @cached_property
    def _redis(self) -> Redis:
        return Redis.from_url(self._url, decode_responses=True)

    # -- producer side ----------------------------------------------------

    def publish(self, topic: str, envelope: dict[str, Any]) -> str:
        message_id = self._redis.xadd(
            name=stream_key(topic),
            fields={ENVELOPE_FIELD: json.dumps(envelope, default=str)},
            maxlen=self._maxlen,
            approximate=True,
        )
        return str(message_id)

    # -- consumer side ----------------------------------------------------

    def create_group(self, topic: str, group: str) -> None:
        try:
            # id="$" starts at the tail: subscribing must not replay the backlog.
            # mkstream creates the stream so an agent can start before its producer.
            self._redis.xgroup_create(
                name=stream_key(topic), groupname=group, id="$", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def subscribe(
        self,
        topic: str,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 0,
    ) -> list[BusMessage]:
        self.create_group(topic, group)

        claimed = self._drain_own_pending(topic, group, consumer, count)
        if len(claimed) >= count:
            return claimed

        claimed.extend(self._read_new(topic, group, consumer, count - len(claimed), block_ms))
        return claimed

    def ack(self, topic: str, group: str, message_id: str) -> None:
        acked = self._redis.xack(stream_key(topic), group, message_id)
        if not acked:
            raise KeyError(f"{message_id!r} is not pending for group {group!r} on {topic!r}")

    def nack(self, topic: str, group: str, message_id: str) -> None:
        """Decline to ack, leaving the entry pending for redelivery.

        Verifies the entry is actually pending so a mistaken nack fails as
        loudly as a mistaken ack, rather than silently doing nothing.
        """
        entries = cast(
            list[PendingEntry],
            self._redis.xpending_range(
                name=stream_key(topic),
                groupname=group,
                min=message_id,
                max=message_id,
                count=1,
            ),
        )
        if not entries:
            raise KeyError(f"{message_id!r} is not pending for group {group!r} on {topic!r}")

    def pending(self, topic: str, group: str) -> list[BusMessage]:
        entries = cast(
            list[PendingEntry],
            self._redis.xpending_range(
                name=stream_key(topic), groupname=group, min="-", max="+", count=1000
            ),
        )
        if not entries:
            return []

        by_id = {str(e["message_id"]): int(e["times_delivered"]) for e in entries}
        return self._fetch(topic, by_id)

    def reclaim(
        self, topic: str, group: str, consumer: str, *, min_idle_ms: int
    ) -> list[BusMessage]:
        reply = cast(
            tuple[str, list[StreamEntry], list[str]],
            self._redis.xautoclaim(
                name=stream_key(topic),
                groupname=group,
                consumername=consumer,
                min_idle_time=min_idle_ms,
                start_id="0-0",
            ),
        )
        entries = reply[1]
        if not entries:
            return []

        # XAUTOCLAIM returns bodies but not delivery counts; XPENDING has them.
        ids = [message_id for message_id, _fields in entries]
        counts = self._delivery_counts(topic, group, ids)
        return [
            self._to_message(topic, message_id, fields, counts.get(message_id, 1))
            for message_id, fields in entries
            if fields
        ]

    def length(self, topic: str) -> int:
        return int(self._redis.xlen(stream_key(topic)))

    # -- lifecycle --------------------------------------------------------

    def healthy(self) -> bool:
        """True if Redis answers. Used by the health endpoint."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def group_info(self, topic: str, group: str) -> dict[str, Any] | None:
        """Consumer-group stats, or None if the group does not exist.

        Feeds the per-agent lag figure in `/v1/health/agents`.
        """
        try:
            groups = self._redis.xinfo_groups(stream_key(topic))
        except ResponseError:
            return None
        for info in groups:
            if str(info.get("name")) == group:
                return dict(info)
        return None

    def close(self) -> None:
        self._redis.close()

    # -- internals --------------------------------------------------------

    def _drain_own_pending(
        self, topic: str, group: str, consumer: str, count: int
    ) -> list[BusMessage]:
        """Reclaim this consumer's un-acked entries before taking new work."""
        entries = cast(
            list[PendingEntry],
            self._redis.xpending_range(
                name=stream_key(topic),
                groupname=group,
                min="-",
                max="+",
                count=count,
                consumername=consumer,
            ),
        )
        if not entries:
            return []

        ids = [str(entry["message_id"]) for entry in entries]
        # XCLAIM re-delivers the bodies and bumps each entry's delivery counter,
        # which is what lets a repeatedly failing message reach the retry budget.
        claimed = cast(
            list[StreamEntry],
            self._redis.xclaim(
                name=stream_key(topic),
                groupname=group,
                consumername=consumer,
                min_idle_time=0,
                message_ids=cast(list[Any], ids),
            ),
        )
        counts = self._delivery_counts(topic, group, ids)
        return [
            self._to_message(topic, message_id, fields, counts.get(message_id, 1))
            for message_id, fields in claimed
            if fields
        ]

    def _read_new(
        self, topic: str, group: str, consumer: str, count: int, block_ms: int
    ) -> list[BusMessage]:
        try:
            reply = self._redis.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream_key(topic): ">"},
                count=count,
                block=block_ms or None,
            )
        except RedisTimeoutError:
            # A blocking read that reached its deadline means "nothing arrived",
            # which is the normal state of an idle topic — not a failure. Left
            # to propagate it surfaced as an agent-loop error every few seconds
            # on every quiet stream, and cost a second of recovery sleep each
            # time.
            return []

        response = cast(list[tuple[str, list[StreamEntry]]], reply or [])

        messages: list[BusMessage] = []
        for _stream, entries in response:
            messages.extend(
                self._to_message(topic, message_id, fields, 1)
                for message_id, fields in entries
                if fields
            )
        return messages

    def _delivery_counts(self, topic: str, group: str, ids: list[str]) -> dict[str, int]:
        if not ids:
            return {}
        wanted = set(ids)
        entries = cast(
            list[PendingEntry],
            self._redis.xpending_range(
                name=stream_key(topic),
                groupname=group,
                min=min(ids),
                max=max(ids),
                count=len(ids),
            ),
        )
        return {
            str(entry["message_id"]): int(entry["times_delivered"])
            for entry in entries
            if str(entry["message_id"]) in wanted
        }

    def _fetch(self, topic: str, counts: dict[str, int]) -> list[BusMessage]:
        """Load entry bodies for ids we only know from XPENDING."""
        messages: list[BusMessage] = []
        for message_id, delivery_count in sorted(counts.items()):
            entries = cast(
                list[StreamEntry],
                self._redis.xrange(stream_key(topic), min=message_id, max=message_id),
            )
            messages.extend(
                self._to_message(topic, entry_id, fields, delivery_count)
                for entry_id, fields in entries
                if fields
            )
        return messages

    @staticmethod
    def _to_message(
        topic: str, message_id: str, fields: dict[str, str], delivery_count: int
    ) -> BusMessage:
        return BusMessage(
            message_id=message_id,
            topic=topic,
            envelope=json.loads(fields[ENVELOPE_FIELD]),
            delivery_count=delivery_count,
        )
