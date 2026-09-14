"""The verdict gate wired into `agents.base.Agent` (P4, PRD section 8.3).

`tests/test_verdict_gate.py` covers the gate's own decision logic in
isolation. This file covers the integration: that `Agent` actually consults
an injected gate before calling `handle()`, and does the right bus operation
(ack/nack) for each decision — without a database, since the gate is injected
directly rather than built from `Settings.sentinel_gate_enabled` (that flag
defaults False and is covered by `test_gate_off_by_default_does_not_wait`).
"""

from __future__ import annotations

from typing import Any

from agents.base import Agent
from bus.memory import InMemoryBus
from common.envelope import Envelope
from common.verdict_gate import GateResult, VerdictGate
from tests.factories import envelope_for

TOPIC = "reports.ingested"
NO_WAIT = (0.0, 0.0, 0.0)


class RecordingAgent(Agent):
    name = "GATED"
    version = "1.0.0"
    input_topic = TOPIC

    def __init__(self, bus: InMemoryBus, **kwargs: Any) -> None:
        super().__init__(bus, persist=False, backoff=NO_WAIT, **kwargs)
        self.calls = 0

    def handle(self, envelope: Envelope) -> list[Envelope]:
        self.calls += 1
        return []


class _FixedGate:
    """A gate double that always returns the same `GateResult`."""

    def __init__(self, result: GateResult) -> None:
        self._result = result
        self.checked: list[str] = []

    def await_verdict(self, message_id: str) -> GateResult:
        self.checked.append(message_id)
        return self._result


def _publish(bus: InMemoryBus, agent: Agent, topic: str = TOPIC) -> dict[str, Any]:
    bus.create_group(topic, agent.group)
    envelope = envelope_for(topic)
    bus.publish(topic, envelope)
    return envelope


# -- proceed ---------------------------------------------------------------


def test_proceed_decision_runs_handle(memory_bus: InMemoryBus) -> None:
    gate = _FixedGate(
        GateResult(decision="proceed", verdict="pass", waited_ms=1.0, timed_out=False)
    )
    agent = RecordingAgent(memory_bus, verdict_gate=gate)
    _publish(memory_bus, agent)

    result = agent.run_once()[0]

    assert agent.calls == 1
    assert result.handled
    assert gate.checked  # the gate was actually consulted


def test_gate_is_checked_against_the_envelope_message_id(memory_bus: InMemoryBus) -> None:
    gate = _FixedGate(
        GateResult(decision="proceed", verdict="pass", waited_ms=0.0, timed_out=False)
    )
    agent = RecordingAgent(memory_bus, verdict_gate=gate)
    published = _publish(memory_bus, agent)
    agent.run_once()

    assert gate.checked == [published["message_id"]]


# -- drop --------------------------------------------------------------


def test_drop_decision_acks_without_calling_handle(memory_bus: InMemoryBus) -> None:
    gate = _FixedGate(
        GateResult(decision="drop", verdict="fail_hard", waited_ms=5.0, timed_out=False)
    )
    agent = RecordingAgent(memory_bus, verdict_gate=gate)
    _publish(memory_bus, agent)

    result = agent.run_once()[0]

    assert agent.calls == 0
    assert not result.handled
    assert memory_bus.pending(TOPIC, agent.group) == []


# -- defer ---------------------------------------------------------------


def test_defer_decision_nacks_without_calling_handle(memory_bus: InMemoryBus) -> None:
    gate = _FixedGate(GateResult(decision="defer", verdict=None, waited_ms=2000.0, timed_out=True))
    agent = RecordingAgent(memory_bus, verdict_gate=gate)
    _publish(memory_bus, agent)

    result = agent.run_once()[0]

    assert agent.calls == 0
    assert not result.handled
    assert result.error == "verdict_pending"
    # Nacked, not acked: back on the group's retry queue for the next sweep,
    # not lost and not left claimed forever.
    second = agent.run_once()[0]
    assert agent.calls == 0
    assert second.error == "verdict_pending"


def test_deferred_delivery_eventually_reaches_the_poison_pill_budget(
    memory_bus: InMemoryBus,
) -> None:
    """A message Sentinel never verifies must not wait forever - it climbs the
    same delivery-count budget as any other retried delivery."""
    from bus.base import MAX_DELIVERIES

    gate = _FixedGate(GateResult(decision="defer", verdict=None, waited_ms=2000.0, timed_out=True))
    agent = RecordingAgent(memory_bus, verdict_gate=gate)
    _publish(memory_bus, agent)

    for _ in range(MAX_DELIVERIES):
        agent.run_once()

    assert agent.calls == 0
    assert memory_bus.length("deadletter") == 1


# -- the flag defaults off --------------------------------------------------


def test_gate_off_by_default_does_not_wait(memory_bus: InMemoryBus) -> None:
    """No injected gate, persist=False: Agent must behave exactly as it did
    before P4 - `gate_enabled` is False and handle() runs unconditionally."""
    agent = RecordingAgent(memory_bus)
    _publish(memory_bus, agent)

    agent.run_once()

    assert agent.gate_enabled is False
    assert agent.calls == 1


def test_gate_not_built_when_persist_is_false_even_if_flag_is_on(
    memory_bus: InMemoryBus, monkeypatch: Any
) -> None:
    """The gate needs a database (it reads verification_results); a
    persist=False agent must never try to build one, flag or no flag."""
    from common import config

    config.settings.cache_clear()
    monkeypatch.setenv("CIVICAI_SENTINEL_GATE_ENABLED", "true")
    config.settings.cache_clear()
    try:
        agent = RecordingAgent(memory_bus)  # persist=False, forced in __init__
        assert agent.gate_enabled is False
    finally:
        config.settings.cache_clear()


class _EmptyStore:
    """A `VerdictStore` that never has an answer. Only `shadow` mode is
    exercised here, which never calls it - this is purely a type-shaped
    placeholder to construct a real `VerdictGate`."""

    def get_verdict(self, message_id: str) -> str | None:
        return None


def test_gate_is_a_real_verdict_gate_instance_when_enabled(memory_bus: InMemoryBus) -> None:
    gate = VerdictGate(_EmptyStore(), mode="shadow")
    agent = RecordingAgent(memory_bus, verdict_gate=gate)
    assert agent.gate_enabled is True
