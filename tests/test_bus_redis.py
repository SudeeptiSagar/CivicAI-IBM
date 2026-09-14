"""RedisBus against the same guarantees the in-memory double satisfies.

The point of `bus/` is that agent code cannot tell the two apart (PRD section
9.1), so most of this file deliberately mirrors test_bus_memory.py. Where the
two implementations differ internally — Redis has no requeue operation, only a
pending-entries list — the tests assert the *observable* behaviour, which must
match.

Skipped when Redis is unreachable.
"""

from __future__ import annotations

from typing import Any

import pytest

from bus.base import MAX_DELIVERIES, Bus
from bus.redis import RedisBus, stream_key
from tests.conftest import requires_redis
from tests.factories import envelope_for

pytestmark = [pytest.mark.integration, requires_redis]

TOPIC = "reports.ingested"
GROUP = "a1_perception"
CONSUMER = "a1-0"


# -- basics --------------------------------------------------------------


def test_publish_then_subscribe_delivers_the_envelope(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    published = envelope_for(TOPIC)
    redis_bus.publish(TOPIC, published)

    received = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)

    assert len(received) == 1
    assert received[0].envelope == published


def test_subscribe_on_an_empty_topic_returns_nothing(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    assert redis_bus.subscribe(TOPIC, GROUP, CONSUMER) == []


def test_group_starts_at_the_tail(redis_bus: RedisBus) -> None:
    """Subscribing must not replay the backlog."""
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    redis_bus.create_group(TOPIC, GROUP)

    assert redis_bus.subscribe(TOPIC, GROUP, CONSUMER) == []


def test_create_group_is_idempotent(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    redis_bus.create_group(TOPIC, GROUP)

    assert len(redis_bus.subscribe(TOPIC, GROUP, CONSUMER)) == 1


def test_count_limits_a_single_claim(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    for _ in range(5):
        redis_bus.publish(TOPIC, envelope_for(TOPIC))

    assert len(redis_bus.subscribe(TOPIC, GROUP, CONSUMER, count=2)) == 2


def test_ordering_is_preserved(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    sent = [envelope_for(TOPIC) for _ in range(4)]
    for envelope in sent:
        redis_bus.publish(TOPIC, envelope)

    received = redis_bus.subscribe(TOPIC, GROUP, CONSUMER, count=10)

    assert [m.envelope["message_id"] for m in received] == [e["message_id"] for e in sent]


def test_envelope_survives_the_json_round_trip(redis_bus: RedisBus) -> None:
    """Redis stores strings; nothing may be lost or retyped on the way through."""
    redis_bus.create_group(TOPIC, GROUP)
    published = envelope_for(TOPIC)
    redis_bus.publish(TOPIC, published)

    received = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0].envelope

    assert received == published
    assert isinstance(received["confidence"], float)
    assert isinstance(received["payload"]["has_photo"], bool)


# -- consumer groups -----------------------------------------------------


def test_each_group_sees_every_message(redis_bus: RedisBus) -> None:
    """reports.ingested fans out to A1 and to Sentinel (PRD section 9.3)."""
    redis_bus.create_group(TOPIC, "a1_perception")
    redis_bus.create_group(TOPIC, "av_sentinel")
    redis_bus.publish(TOPIC, envelope_for(TOPIC))

    assert len(redis_bus.subscribe(TOPIC, "a1_perception", "a1-0")) == 1
    assert len(redis_bus.subscribe(TOPIC, "av_sentinel", "av-0")) == 1


def test_consumers_in_one_group_do_not_double_handle(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    for _ in range(4):
        redis_bus.publish(TOPIC, envelope_for(TOPIC))

    first = redis_bus.subscribe(TOPIC, GROUP, "a1-0", count=2)
    second = redis_bus.subscribe(TOPIC, GROUP, "a1-1", count=2)

    assert {m.message_id for m in first}.isdisjoint({m.message_id for m in second})


# -- ack, nack, redelivery ----------------------------------------------


def test_claimed_message_stays_pending_until_acked(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    claimed = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0]

    assert len(redis_bus.pending(TOPIC, GROUP)) == 1
    redis_bus.ack(TOPIC, GROUP, claimed.message_id)
    assert redis_bus.pending(TOPIC, GROUP) == []


def test_nack_redelivers(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    claimed = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0]

    redis_bus.nack(TOPIC, GROUP, claimed.message_id)
    redelivered = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)

    assert len(redelivered) == 1
    assert redelivered[0].envelope == claimed.envelope
    assert redelivered[0].delivery_count == 2


def test_delivery_count_climbs_with_each_failure(redis_bus: RedisBus) -> None:
    """Without this the retry budget never trips and a poison pill loops forever."""
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))

    counts = []
    for _ in range(MAX_DELIVERIES):
        message = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
        counts.append(message.delivery_count)
        redis_bus.nack(TOPIC, GROUP, message.message_id)

    assert counts == list(range(1, MAX_DELIVERIES + 1))


def test_poison_pill_is_flagged_at_the_retry_budget(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))

    message = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
    assert not message.is_poison

    for _ in range(MAX_DELIVERIES - 1):
        redis_bus.nack(TOPIC, GROUP, message.message_id)
        message = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0]

    assert message.is_poison


def test_pending_work_is_claimed_before_new_work(redis_bus: RedisBus) -> None:
    """A failing message must not be starved by a busy topic, or it never
    reaches the retry budget."""
    redis_bus.create_group(TOPIC, GROUP)
    first = envelope_for(TOPIC)
    redis_bus.publish(TOPIC, first)

    claimed = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
    redis_bus.nack(TOPIC, GROUP, claimed.message_id)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))

    received = redis_bus.subscribe(TOPIC, GROUP, CONSUMER, count=1)

    assert received[0].envelope["message_id"] == first["message_id"]


def test_acked_message_is_not_redelivered(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    claimed = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
    redis_bus.ack(TOPIC, GROUP, claimed.message_id)

    assert redis_bus.subscribe(TOPIC, GROUP, CONSUMER) == []


def test_acking_an_unknown_delivery_raises(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    with pytest.raises(KeyError):
        redis_bus.ack(TOPIC, GROUP, "999999999-0")


def test_double_ack_raises(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    claimed = redis_bus.subscribe(TOPIC, GROUP, CONSUMER)[0]

    redis_bus.ack(TOPIC, GROUP, claimed.message_id)
    with pytest.raises(KeyError):
        redis_bus.ack(TOPIC, GROUP, claimed.message_id)


def test_nacking_an_unknown_delivery_raises(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    with pytest.raises(KeyError):
        redis_bus.nack(TOPIC, GROUP, "999999999-0")


# -- reclaim (XAUTOCLAIM) -----------------------------------------------


def test_reclaim_takes_over_a_stalled_delivery(redis_bus: RedisBus) -> None:
    """A consumer that dies mid-handler must not strand its work."""
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    redis_bus.subscribe(TOPIC, GROUP, "a1-dead")

    reclaimed = redis_bus.reclaim(TOPIC, GROUP, "a1-alive", min_idle_ms=0)

    assert len(reclaimed) == 1
    assert reclaimed[0].delivery_count == 2


def test_reclaim_leaves_fresh_deliveries_alone(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    redis_bus.subscribe(TOPIC, GROUP, "a1-busy")

    assert redis_bus.reclaim(TOPIC, GROUP, "a1-other", min_idle_ms=60_000) == []


# -- blocking and diagnostics -------------------------------------------


def test_blocking_read_gives_up_at_the_deadline(redis_bus: RedisBus) -> None:
    redis_bus.create_group(TOPIC, GROUP)
    assert redis_bus.subscribe(TOPIC, GROUP, CONSUMER, block_ms=50) == []


def test_long_blocking_read_returns_empty_rather_than_raising(redis_bus: RedisBus) -> None:
    """Regression: a blocking read that reaches its deadline raises
    redis.TimeoutError. Left to propagate it surfaced as an "agent loop error"
    every few seconds on every idle topic, each costing a second of recovery
    sleep. An idle stream is the normal state, not a failure.

    The agents block for 5s, which is where this first showed up; 2s keeps the
    test quick while still exceeding the client's read deadline."""
    redis_bus.create_group(TOPIC, GROUP)

    assert redis_bus.subscribe(TOPIC, GROUP, CONSUMER, block_ms=2000) == []


def test_blocking_read_still_delivers_when_a_message_arrives(redis_bus: RedisBus) -> None:
    """The timeout handling must not swallow real messages."""
    import threading

    redis_bus.create_group(TOPIC, GROUP)
    received: list[int] = []

    def consume() -> None:
        received.append(len(redis_bus.subscribe(TOPIC, GROUP, CONSUMER, block_ms=5000)))

    reader = threading.Thread(target=consume)
    reader.start()
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    reader.join(timeout=15)

    assert not reader.is_alive()
    assert received == [1]


def test_length_counts_published_messages(redis_bus: RedisBus) -> None:
    for _ in range(3):
        redis_bus.publish(TOPIC, envelope_for(TOPIC))

    assert redis_bus.length(TOPIC) == 3
    assert redis_bus.length("incidents.routed") == 0


def test_group_info_reports_backlog(redis_bus: RedisBus) -> None:
    """Feeds the per-agent lag figure in /v1/health/agents."""
    redis_bus.create_group(TOPIC, GROUP)
    redis_bus.publish(TOPIC, envelope_for(TOPIC))
    redis_bus.subscribe(TOPIC, GROUP, CONSUMER)

    info = redis_bus.group_info(TOPIC, GROUP)

    assert info is not None
    assert info["pending"] == 1


def test_group_info_is_none_for_an_unknown_stream(redis_bus: RedisBus) -> None:
    assert redis_bus.group_info("incidents.unrouted", GROUP) is None


def test_healthy_reports_true_when_redis_answers(redis_bus: RedisBus) -> None:
    assert redis_bus.healthy() is True


def test_streams_are_namespaced() -> None:
    """CivicAI must be able to share a Redis without colliding."""
    assert stream_key("reports.ingested").startswith("civicai:stream:")


def test_redis_bus_implements_the_abstraction() -> None:
    assert issubclass(RedisBus, Bus)
    assert not getattr(RedisBus, "__abstractmethods__", None)


# -- parity with the in-memory double ------------------------------------


def _behaviour(bus: Bus) -> dict[str, Any]:
    """Exercise one nack/redeliver/ack cycle and report what was observed."""
    topic, group, consumer = "incidents.updated", "parity", "p-0"
    bus.create_group(topic, group)
    bus.publish(topic, envelope_for(topic))

    first = bus.subscribe(topic, group, consumer)[0]
    bus.nack(topic, group, first.message_id)
    second = bus.subscribe(topic, group, consumer)[0]
    bus.ack(topic, group, second.message_id)

    return {
        "first_count": first.delivery_count,
        "second_count": second.delivery_count,
        "same_envelope": first.envelope == second.envelope,
        "pending_after_ack": len(bus.pending(topic, group)),
        "after_ack": len(bus.subscribe(topic, group, consumer)),
    }


def test_redis_and_memory_behave_identically(redis_bus: RedisBus) -> None:
    """Agent code is written against Bus; if these diverge, an agent that works
    in tests can fail in production."""
    from bus.memory import InMemoryBus

    assert _behaviour(redis_bus) == _behaviour(InMemoryBus())
