"""The in-memory bus against the guarantees in PRD section 9.4.

The point of these tests is the weak guarantees, not the happy path: at-least-
once means redelivery is normal, and every agent is written assuming it.
"""

from __future__ import annotations

import threading

import pytest

from bus.base import MAX_DELIVERIES, Bus
from bus.memory import InMemoryBus
from common.envelope import Envelope
from common.idempotency import InMemoryIdempotencyStore, key_for
from tests.factories import envelope_for

TOPIC = "reports.ingested"
GROUP = "a1_perception"
CONSUMER = "a1-0"


@pytest.fixture
def bus() -> InMemoryBus:
    return InMemoryBus()


# -- basics --------------------------------------------------------------


def test_publish_then_subscribe_delivers_the_envelope(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    published = envelope_for(TOPIC)
    bus.publish(TOPIC, published)

    received = bus.subscribe(TOPIC, GROUP, CONSUMER)

    assert len(received) == 1
    assert received[0].envelope == published


def test_subscribe_on_an_empty_topic_returns_nothing(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    assert bus.subscribe(TOPIC, GROUP, CONSUMER) == []


def test_group_starts_at_the_tail(bus: InMemoryBus) -> None:
    """Subscribing must not replay history; replay is an explicit operation."""
    bus.publish(TOPIC, envelope_for(TOPIC))
    bus.create_group(TOPIC, GROUP)

    assert bus.subscribe(TOPIC, GROUP, CONSUMER) == []


def test_create_group_is_idempotent(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))
    bus.create_group(TOPIC, GROUP)

    assert len(bus.subscribe(TOPIC, GROUP, CONSUMER)) == 1


def test_count_limits_a_single_claim(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    for _ in range(5):
        bus.publish(TOPIC, envelope_for(TOPIC))

    assert len(bus.subscribe(TOPIC, GROUP, CONSUMER, count=2)) == 2


def test_ordering_is_preserved_within_a_topic(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    sent = [envelope_for(TOPIC) for _ in range(4)]
    for envelope in sent:
        bus.publish(TOPIC, envelope)

    received = bus.subscribe(TOPIC, GROUP, CONSUMER, count=10)

    assert [m.envelope["message_id"] for m in received] == [e["message_id"] for e in sent]


# -- consumer groups -----------------------------------------------------


def test_each_group_sees_every_message(bus: InMemoryBus) -> None:
    """reports.ingested fans out to A1 and to Sentinel (PRD section 9.3)."""
    bus.create_group(TOPIC, "a1_perception")
    bus.create_group(TOPIC, "av_sentinel")
    bus.publish(TOPIC, envelope_for(TOPIC))

    assert len(bus.subscribe(TOPIC, "a1_perception", "a1-0")) == 1
    assert len(bus.subscribe(TOPIC, "av_sentinel", "av-0")) == 1


def test_consumers_in_one_group_do_not_double_handle(bus: InMemoryBus) -> None:
    """Scaling an agent to two workers must split the load, not duplicate it."""
    bus.create_group(TOPIC, GROUP)
    for _ in range(4):
        bus.publish(TOPIC, envelope_for(TOPIC))

    first = bus.subscribe(TOPIC, GROUP, "a1-0", count=2)
    second = bus.subscribe(TOPIC, GROUP, "a1-1", count=2)

    assert {m.message_id for m in first}.isdisjoint({m.message_id for m in second})


def test_subscribing_to_an_unknown_group_creates_it(bus: InMemoryBus) -> None:
    bus.subscribe(TOPIC, "brand_new", CONSUMER)
    bus.publish(TOPIC, envelope_for(TOPIC))
    assert len(bus.subscribe(TOPIC, "brand_new", CONSUMER)) == 1


# -- ack, nack, redelivery ----------------------------------------------


def test_claimed_message_stays_pending_until_acked(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))
    claimed = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]

    assert len(bus.pending(TOPIC, GROUP)) == 1
    bus.ack(TOPIC, GROUP, claimed.message_id)
    assert bus.pending(TOPIC, GROUP) == []


def test_nack_redelivers(bus: InMemoryBus) -> None:
    """A crash mid-handler must lose nothing."""
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))
    claimed = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]

    bus.nack(TOPIC, GROUP, claimed.message_id)
    redelivered = bus.subscribe(TOPIC, GROUP, CONSUMER)

    assert len(redelivered) == 1
    assert redelivered[0].envelope == claimed.envelope
    assert redelivered[0].delivery_count == 2


def test_delivery_count_climbs_with_each_failure(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))

    counts = []
    for _ in range(MAX_DELIVERIES):
        message = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
        counts.append(message.delivery_count)
        bus.nack(TOPIC, GROUP, message.message_id)

    assert counts == list(range(1, MAX_DELIVERIES + 1))


def test_poison_pill_is_flagged_at_the_retry_budget(bus: InMemoryBus) -> None:
    """PRD section 9.4: 3 attempts, then quarantine — never an endless requeue."""
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))

    message = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
    assert not message.is_poison

    for _ in range(MAX_DELIVERIES - 1):
        bus.nack(TOPIC, GROUP, message.message_id)
        message = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]

    assert message.is_poison


def test_acking_an_unknown_delivery_raises(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    with pytest.raises(KeyError):
        bus.ack(TOPIC, GROUP, "999-0")


def test_double_ack_raises(bus: InMemoryBus) -> None:
    """Silently tolerating a double ack would hide a handler bug."""
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))
    claimed = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]

    bus.ack(TOPIC, GROUP, claimed.message_id)
    with pytest.raises(KeyError):
        bus.ack(TOPIC, GROUP, claimed.message_id)


def test_acked_message_is_not_redelivered(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))
    claimed = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
    bus.ack(TOPIC, GROUP, claimed.message_id)

    assert bus.subscribe(TOPIC, GROUP, CONSUMER) == []


# -- reclaim (XAUTOCLAIM) -----------------------------------------------


def test_reclaim_takes_over_a_stalled_delivery(bus: InMemoryBus) -> None:
    """A consumer that dies mid-handler must not strand its work."""
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))
    bus.subscribe(TOPIC, GROUP, "a1-dead")

    reclaimed = bus.reclaim(TOPIC, GROUP, "a1-alive", min_idle_ms=0)

    assert len(reclaimed) == 1
    assert reclaimed[0].delivery_count == 2


def test_reclaim_leaves_fresh_deliveries_alone(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))
    bus.subscribe(TOPIC, GROUP, "a1-busy")

    assert bus.reclaim(TOPIC, GROUP, "a1-other", min_idle_ms=60_000) == []


# -- blocking reads ------------------------------------------------------


def test_blocking_read_wakes_on_publish(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    received: list[int] = []

    def consume() -> None:
        received.append(len(bus.subscribe(TOPIC, GROUP, CONSUMER, block_ms=5_000)))

    reader = threading.Thread(target=consume)
    reader.start()
    bus.publish(TOPIC, envelope_for(TOPIC))
    reader.join(timeout=10)

    assert not reader.is_alive()
    assert received == [1]


def test_blocking_read_gives_up_at_the_deadline(bus: InMemoryBus) -> None:
    bus.create_group(TOPIC, GROUP)
    assert bus.subscribe(TOPIC, GROUP, CONSUMER, block_ms=50) == []


# -- interface conformance ----------------------------------------------


def test_in_memory_bus_implements_the_abstraction() -> None:
    """Agents depend on Bus, never on InMemoryBus (PRD section 9.1)."""
    assert issubclass(InMemoryBus, Bus)
    assert not getattr(InMemoryBus, "__abstractmethods__", None)


def test_length_counts_published_messages(bus: InMemoryBus) -> None:
    for _ in range(3):
        bus.publish(TOPIC, envelope_for(TOPIC))
    assert bus.length(TOPIC) == 3
    assert bus.length("incidents.routed") == 0


def test_published_envelope_is_copied_not_aliased(bus: InMemoryBus) -> None:
    """A producer mutating its dict after publishing must not rewrite history."""
    envelope = envelope_for(TOPIC)
    bus.publish(TOPIC, envelope)
    envelope["confidence"] = 0.0

    assert bus.messages(TOPIC)[0]["confidence"] != 0.0


# -- redelivery meets idempotency ---------------------------------------


def test_redelivery_is_a_no_op_for_an_idempotent_handler(bus: InMemoryBus) -> None:
    """At-least-once plus an idempotent handler equals effectively-once, which
    is the contract every agent in PRD section 7 is written against."""
    store = InMemoryIdempotencyStore()
    handled: list[str] = []

    def handle(envelope: dict[str, object]) -> str:
        key = key_for(Envelope.from_dict(dict(envelope)))
        if store.seen(key):
            return str(store.result_for(key))
        handled.append(key)
        result = f"processed:{key}"
        store.record(key, result)
        return result

    bus.create_group(TOPIC, GROUP)
    bus.publish(TOPIC, envelope_for(TOPIC))

    first = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
    first_result = handle(first.envelope)
    bus.nack(TOPIC, GROUP, first.message_id)

    second = bus.subscribe(TOPIC, GROUP, CONSUMER)[0]
    second_result = handle(second.envelope)

    assert first_result == second_result
    assert len(handled) == 1
