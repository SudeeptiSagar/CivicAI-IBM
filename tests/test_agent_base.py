"""The agent runtime against the PRD section 7 contract.

Runs entirely on the in-memory bus with persistence off, so the contract is
verified without any infrastructure. The database side of the runtime is
covered by the integration tests in test_pipeline_m0.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.base import Agent, AgentResult, SkipSignal
from bus.base import MAX_DELIVERIES
from bus.memory import InMemoryBus
from common.envelope import Envelope
from tests.factories import envelope_for, payload_for

TOPIC = "reports.ingested"

# Zero backoff: these tests exercise the retry policy, not the waiting.
NO_WAIT = (0.0, 0.0, 0.0)


class RecordingAgent(Agent):
    """Emits one reports.understood event per input."""

    name = "TEST"
    version = "1.0.0"
    input_topic = TOPIC

    def __init__(self, bus: InMemoryBus, **kwargs: Any) -> None:
        super().__init__(bus, persist=False, backoff=NO_WAIT, **kwargs)
        self.seen: list[Envelope] = []

    def handle(self, envelope: Envelope) -> list[Envelope]:
        self.seen.append(envelope)
        return [
            envelope.derive(
                topic="reports.understood",
                producer=self.producer,
                confidence=0.8,
                rationale="test agent output",
                payload=payload_for("reports.understood"),
            )
        ]


class ExplodingAgent(Agent):
    """Always fails. Used to exercise the retry budget."""

    name = "BOOM"
    version = "1.0.0"
    input_topic = TOPIC

    def __init__(self, bus: InMemoryBus, **kwargs: Any) -> None:
        super().__init__(bus, persist=False, backoff=NO_WAIT, **kwargs)
        self.attempts = 0

    def handle(self, envelope: Envelope) -> list[Envelope]:
        self.attempts += 1
        raise RuntimeError("handler exploded")


class FlakyAgent(Agent):
    """Fails once, then succeeds. Exercises retry-then-recover."""

    name = "FLAKY"
    version = "1.0.0"
    input_topic = TOPIC

    def __init__(self, bus: InMemoryBus, **kwargs: Any) -> None:
        super().__init__(bus, persist=False, backoff=NO_WAIT, **kwargs)
        self.attempts = 0

    def handle(self, envelope: Envelope) -> list[Envelope]:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("transient")
        return []


class SkippingAgent(Agent):
    """Always skips."""

    name = "SKIP"
    version = "1.0.0"
    input_topic = TOPIC

    def __init__(self, bus: InMemoryBus, **kwargs: Any) -> None:
        super().__init__(bus, persist=False, backoff=NO_WAIT, **kwargs)

    def handle(self, envelope: Envelope) -> list[Envelope]:
        raise SkipSignal("nothing_to_do", "no media and no text")


def _publish(bus: InMemoryBus, agent: Agent, topic: str = TOPIC) -> dict[str, Any]:
    """Publish one valid envelope after the agent's group exists."""
    bus.create_group(topic, agent.group)
    envelope = envelope_for(topic)
    bus.publish(topic, envelope)
    return envelope


# -- the happy path ------------------------------------------------------


def test_agent_consumes_and_emits(memory_bus: InMemoryBus) -> None:
    agent = RecordingAgent(memory_bus)
    _publish(memory_bus, agent)

    results = agent.run_once()

    assert len(results) == 1
    assert results[0].handled
    assert len(results[0].emitted) == 1
    assert memory_bus.length("reports.understood") == 1


def test_emitted_message_keeps_the_trace(memory_bus: InMemoryBus) -> None:
    """The rule that makes any incident replayable to its source."""
    agent = RecordingAgent(memory_bus)
    published = _publish(memory_bus, agent)

    emitted = agent.run_once()[0].emitted[0]

    assert str(emitted.trace_id) == published["trace_id"]
    assert str(emitted.causation_id) == published["message_id"]


def test_successful_handling_acks(memory_bus: InMemoryBus) -> None:
    agent = RecordingAgent(memory_bus)
    _publish(memory_bus, agent)
    agent.run_once()

    assert memory_bus.pending(TOPIC, agent.group) == []


def test_producer_block_identifies_the_agent(memory_bus: InMemoryBus) -> None:
    agent = RecordingAgent(memory_bus)
    _publish(memory_bus, agent)

    emitted = agent.run_once()[0].emitted[0]

    assert emitted.producer.agent == "TEST"
    assert emitted.producer.version == "1.0.0"


def test_run_once_on_an_empty_topic_does_nothing(memory_bus: InMemoryBus) -> None:
    agent = RecordingAgent(memory_bus)
    assert agent.run_once() == []


# -- skipping ------------------------------------------------------------


def test_skip_emits_a_skipped_event(memory_bus: InMemoryBus) -> None:
    """PRD section 7: one output per input, or a *.skipped with a reason."""
    agent = SkippingAgent(memory_bus)
    _publish(memory_bus, agent)

    result = agent.run_once()[0]

    assert result.skipped
    assert result.handled
    assert memory_bus.length(f"{TOPIC}.skipped") == 1


def test_skipped_event_carries_the_reason(memory_bus: InMemoryBus) -> None:
    agent = SkippingAgent(memory_bus)
    _publish(memory_bus, agent)
    agent.run_once()

    payload = memory_bus.messages(f"{TOPIC}.skipped")[0]["payload"]

    assert payload["reason_code"] == "nothing_to_do"
    assert payload["reason"] == "no media and no text"


def test_skipped_event_keeps_the_trace(memory_bus: InMemoryBus) -> None:
    agent = SkippingAgent(memory_bus)
    published = _publish(memory_bus, agent)
    agent.run_once()

    skipped = memory_bus.messages(f"{TOPIC}.skipped")[0]

    assert skipped["trace_id"] == published["trace_id"]
    assert skipped["causation_id"] == published["message_id"]


def test_skip_acks_rather_than_retrying(memory_bus: InMemoryBus) -> None:
    """A skip is a decision, not a failure."""
    agent = SkippingAgent(memory_bus)
    _publish(memory_bus, agent)
    agent.run_once()

    assert memory_bus.pending(TOPIC, agent.group) == []


# -- retries and deadletter ----------------------------------------------


def test_handler_is_retried_to_the_budget(memory_bus: InMemoryBus) -> None:
    """PRD section 9.4: three attempts before giving up."""
    agent = ExplodingAgent(memory_bus)
    _publish(memory_bus, agent)

    agent.run_once()

    assert agent.attempts == MAX_DELIVERIES


def test_transient_failure_recovers_without_deadlettering(memory_bus: InMemoryBus) -> None:
    agent = FlakyAgent(memory_bus)
    _publish(memory_bus, agent)

    result = agent.run_once()[0]

    assert result.handled
    assert agent.attempts == 2
    assert memory_bus.length("deadletter") == 0


def test_exhausted_retries_deadletter(memory_bus: InMemoryBus) -> None:
    agent = ExplodingAgent(memory_bus)
    _publish(memory_bus, agent)

    result = agent.run_once()[0]

    assert not result.handled
    assert memory_bus.length("deadletter") == 1


def test_deadletter_records_the_error_chain(memory_bus: InMemoryBus) -> None:
    """Fail loudly: the deadletter has to say what actually went wrong."""
    agent = ExplodingAgent(memory_bus)
    _publish(memory_bus, agent)
    agent.run_once()

    payload = memory_bus.messages("deadletter")[0]["payload"]

    assert payload["agent"] == "BOOM"
    assert len(payload["error_chain"]) == MAX_DELIVERIES
    assert "handler exploded" in payload["error_chain"][0]
    assert payload["original_envelope"]["topic"] == TOPIC


def test_deadlettered_message_is_acked_not_requeued(memory_bus: InMemoryBus) -> None:
    """Poison-pill protection: re-queueing forever is what PRD 9.4 forbids."""
    agent = ExplodingAgent(memory_bus)
    _publish(memory_bus, agent)
    agent.run_once()

    assert memory_bus.pending(TOPIC, agent.group) == []
    assert agent.run_once() == []


def test_unparseable_envelope_is_deadlettered_not_retried(memory_bus: InMemoryBus) -> None:
    """Sentinel should have caught it upstream; retrying cannot help."""
    agent = RecordingAgent(memory_bus)
    memory_bus.create_group(TOPIC, agent.group)
    memory_bus.publish(TOPIC, {"not": "an envelope"})

    result = agent.run_once()[0]

    assert not result.handled
    assert agent.seen == []
    assert memory_bus.length("deadletter") == 1


def test_poison_pill_from_redelivery_is_deadlettered(memory_bus: InMemoryBus) -> None:
    """A message that burned its budget across restarts must not loop forever."""
    agent = RecordingAgent(memory_bus)
    memory_bus.create_group(TOPIC, agent.group)
    memory_bus.publish(TOPIC, envelope_for(TOPIC))

    for _ in range(MAX_DELIVERIES - 1):
        claimed = memory_bus.subscribe(TOPIC, agent.group, agent.consumer)[0]
        memory_bus.nack(TOPIC, agent.group, claimed.message_id)

    result = agent.run_once()[0]

    assert not result.handled
    assert result.error == "poison pill"
    assert agent.seen == []
    assert memory_bus.length("deadletter") == 1


# -- identity ------------------------------------------------------------


def test_each_agent_gets_its_own_consumer_group() -> None:
    """Every agent must see every event on its input topic (PRD section 9.3)."""
    bus = InMemoryBus()
    first = RecordingAgent(bus)
    second = SkippingAgent(bus)

    assert first.group != second.group


def test_both_agents_receive_the_same_message() -> None:
    bus = InMemoryBus()
    first = RecordingAgent(bus)
    second = SkippingAgent(bus)
    bus.create_group(TOPIC, first.group)
    bus.create_group(TOPIC, second.group)
    bus.publish(TOPIC, envelope_for(TOPIC))

    assert len(first.run_once()) == 1
    assert len(second.run_once()) == 1


def test_agent_result_reports_emitted_messages(memory_bus: InMemoryBus) -> None:
    agent = RecordingAgent(memory_bus)
    _publish(memory_bus, agent)

    result = agent.run_once()[0]

    assert isinstance(result, AgentResult)
    assert [e.topic for e in result.emitted] == ["reports.understood"]


def test_stop_ends_the_forever_loop(memory_bus: InMemoryBus) -> None:
    """A running agent must shut down when asked, or a container never stops."""
    import threading

    agent = RecordingAgent(memory_bus)
    loop = threading.Thread(target=agent.run_forever, kwargs={"block_ms": 10}, daemon=True)
    loop.start()

    agent.stop()
    loop.join(timeout=5)

    assert not loop.is_alive()


@pytest.mark.parametrize("agent_type", [RecordingAgent, SkippingAgent, ExplodingAgent])
def test_every_agent_emits_schema_valid_messages(
    agent_type: type[Agent], memory_bus: InMemoryBus
) -> None:
    """Whatever an agent emits - output, skip or deadletter - must pass L1."""
    from agents.av_sentinel.layers.structural import verify_structural

    agent = agent_type(memory_bus)
    _publish(memory_bus, agent)
    agent.run_once()

    for topic in memory_bus.topics():
        if topic == TOPIC:
            continue
        for envelope in memory_bus.messages(topic):
            verdict = verify_structural(envelope)
            assert verdict.passed, (topic, verdict.reasons)
