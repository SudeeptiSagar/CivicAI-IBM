"""AV Sentinel's runtime behaviour (PRD section 8).

Structural verification itself is covered in test_structural.py. This file is
about what Sentinel *does* with a verdict: what it emits, what it quarantines,
and — most importantly — what it refuses to touch.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.av_sentinel.agent import SELF_TOPICS, VERIFIED_TOPICS, Sentinel
from agents.av_sentinel.layers.structural import verify_structural
from bus.memory import InMemoryBus
from common.topics import TOPICS
from tests.factories import envelope_for

TOPIC = "reports.ingested"


@pytest.fixture
def sentinel(memory_bus: InMemoryBus) -> Sentinel:
    """A Sentinel with persistence off, watching one topic."""
    return Sentinel(memory_bus, persist=False, topics=(TOPIC,))


def _publish(bus: InMemoryBus, envelope: dict[str, Any], topic: str = TOPIC) -> None:
    from agents.av_sentinel.agent import GROUP

    bus.create_group(topic, GROUP)
    bus.publish(topic, envelope)


# -- coverage ------------------------------------------------------------


def test_sentinel_watches_every_concrete_topic_but_its_own() -> None:
    """PRD section 8: subscribes to every topic. Its own output is excluded so
    a verdict does not produce a verdict, forever."""
    assert set(VERIFIED_TOPICS) >= TOPICS - SELF_TOPICS
    assert SELF_TOPICS.isdisjoint(VERIFIED_TOPICS)


def test_sentinel_watches_the_skip_family() -> None:
    """Regression: skip topics are patterned, so enumerating only the concrete
    topics left every `*.skipped` event unverified — a real agent decision with
    no verdict, which is exactly what Sentinel exists to prevent. Caught by
    running the live stack, not by a test with an explicit topic list."""
    from common.topics import skipped_topic_for

    for topic in TOPICS - SELF_TOPICS:
        assert skipped_topic_for(topic) in VERIFIED_TOPICS


def test_a_default_sentinel_verifies_an_agents_skip(memory_bus: InMemoryBus) -> None:
    """The end-to-end version of the same regression: an agent skips, and a
    Sentinel configured with its real defaults must produce a verdict for it."""
    from agents.base import Agent, SkipSignal
    from common.envelope import Envelope

    class Skipper(Agent):
        name = "SKIP"
        version = "1.0.0"
        input_topic = TOPIC

        def handle(self, envelope: Envelope) -> list[Envelope]:
            raise SkipSignal("nothing_to_do", "no media and no text")

    agent = Skipper(memory_bus, persist=False, backoff=(0.0,))
    sentinel = Sentinel(memory_bus, persist=False)  # real defaults, no topic list
    sentinel.subscribe_all()

    memory_bus.create_group(TOPIC, agent.group)
    memory_bus.publish(TOPIC, envelope_for(TOPIC))
    agent.run_once()

    verified = {v.verdict for v in sentinel.run_once()}

    assert memory_bus.length(f"{TOPIC}.skipped") == 1
    assert verified == {"pass"}


def test_self_topics_are_sentinel_outputs() -> None:
    assert {"verification.results", "sentinel.alert"} == SELF_TOPICS


def test_subscribe_all_creates_a_group_per_topic(memory_bus: InMemoryBus) -> None:
    sentinel = Sentinel(memory_bus, persist=False)
    sentinel.subscribe_all()

    for topic in VERIFIED_TOPICS:
        memory_bus.publish(topic, envelope_for(topic))

    assert len(sentinel.run_once()) == len(VERIFIED_TOPICS)


# -- verdicts ------------------------------------------------------------


def test_valid_message_passes(sentinel: Sentinel, memory_bus: InMemoryBus) -> None:
    _publish(memory_bus, envelope_for(TOPIC))

    verdicts = sentinel.run_once()

    assert [v.verdict for v in verdicts] == ["pass"]


def test_verdict_is_published(sentinel: Sentinel, memory_bus: InMemoryBus) -> None:
    _publish(memory_bus, envelope_for(TOPIC))
    sentinel.run_once()

    # Two verdicts for reports.ingested since P4: L1 (structural), then L2
    # (A0 invariants — reports.ingested has rules, see layers/invariants.py).
    assert memory_bus.length("verification.results") == 2


def test_published_verdict_is_itself_schema_valid(
    sentinel: Sentinel, memory_bus: InMemoryBus
) -> None:
    """Sentinel's own output has to satisfy the contract it enforces."""
    _publish(memory_bus, envelope_for(TOPIC))
    sentinel.run_once()

    verdict_envelope = memory_bus.messages("verification.results")[0]

    assert verify_structural(verdict_envelope).passed


def test_verdict_carries_the_subject_trace(sentinel: Sentinel, memory_bus: InMemoryBus) -> None:
    """A verdict has to be findable from the trace it judges."""
    subject = envelope_for(TOPIC)
    _publish(memory_bus, subject)
    sentinel.run_once()

    verdict_envelope = memory_bus.messages("verification.results")[0]

    assert verdict_envelope["trace_id"] == subject["trace_id"]
    assert verdict_envelope["causation_id"] == subject["message_id"]
    assert verdict_envelope["payload"]["message_id"] == subject["message_id"]


def test_verdict_names_the_producing_agent(sentinel: Sentinel, memory_bus: InMemoryBus) -> None:
    _publish(memory_bus, envelope_for(TOPIC))
    sentinel.run_once()

    assert memory_bus.messages("verification.results")[0]["payload"]["agent"] == "A0"


def test_verdict_reports_layer_l1_only(sentinel: Sentinel, memory_bus: InMemoryBus) -> None:
    """Only L1 runs in P1; claiming more would overstate the coverage."""
    _publish(memory_bus, envelope_for(TOPIC))
    sentinel.run_once()

    assert memory_bus.messages("verification.results")[0]["payload"]["layer"] == "L1"


# -- quarantine ----------------------------------------------------------


def _broken() -> dict[str, Any]:
    envelope = envelope_for("reports.understood")
    envelope["payload"]["embedding"] = [0.1] * 512
    return envelope


def test_invalid_message_fails_hard(memory_bus: InMemoryBus) -> None:
    sentinel = Sentinel(memory_bus, persist=False, topics=("reports.understood",))
    _publish(memory_bus, _broken(), "reports.understood")

    verdicts = sentinel.run_once()

    assert [v.verdict for v in verdicts] == ["fail_hard"]


def test_failed_message_is_quarantined(memory_bus: InMemoryBus) -> None:
    """PRD section 8.2: downstream never sees a fail_hard envelope."""
    sentinel = Sentinel(memory_bus, persist=False, topics=("reports.understood",))
    _publish(memory_bus, _broken(), "reports.understood")
    sentinel.run_once()

    assert memory_bus.length("quarantine.reports.understood") == 1


def test_quarantine_preserves_the_original_envelope(memory_bus: InMemoryBus) -> None:
    """Triage needs the thing that failed, not a summary of it."""
    sentinel = Sentinel(memory_bus, persist=False, topics=("reports.understood",))
    broken = _broken()
    _publish(memory_bus, broken, "reports.understood")
    sentinel.run_once()

    payload = memory_bus.messages("quarantine.reports.understood")[0]["payload"]

    assert payload["original_envelope"] == broken
    assert payload["reasons"]


def test_quarantine_message_is_schema_valid(memory_bus: InMemoryBus) -> None:
    sentinel = Sentinel(memory_bus, persist=False, topics=("reports.understood",))
    _publish(memory_bus, _broken(), "reports.understood")
    sentinel.run_once()

    quarantined = memory_bus.messages("quarantine.reports.understood")[0]

    assert verify_structural(quarantined).passed


def test_passing_message_is_not_quarantined(sentinel: Sentinel, memory_bus: InMemoryBus) -> None:
    _publish(memory_bus, envelope_for(TOPIC))
    sentinel.run_once()

    assert "quarantine.reports.ingested" not in memory_bus.topics()


def test_unparseable_envelope_still_gets_a_verdict(memory_bus: InMemoryBus) -> None:
    """Sentinel must survive exactly the input it exists to catch."""
    sentinel = Sentinel(memory_bus, persist=False, topics=(TOPIC,))
    _publish(memory_bus, {"total": "garbage"})

    verdicts = sentinel.run_once()

    assert [v.verdict for v in verdicts] == ["fail_hard"]
    assert memory_bus.length("verification.results") == 1


# -- what Sentinel must not do -------------------------------------------


def test_sentinel_does_not_mutate_the_message_it_checks(
    sentinel: Sentinel, memory_bus: InMemoryBus
) -> None:
    """PRD section 8.2: it can flag and block, but never fix."""
    subject = envelope_for(TOPIC)
    before = dict(subject)
    _publish(memory_bus, subject)
    sentinel.run_once()

    assert memory_bus.messages(TOPIC)[0] == before


def test_sentinel_does_not_repair_a_broken_message(memory_bus: InMemoryBus) -> None:
    sentinel = Sentinel(memory_bus, persist=False, topics=("reports.understood",))
    broken = _broken()
    _publish(memory_bus, broken, "reports.understood")
    sentinel.run_once()

    quarantined = memory_bus.messages("quarantine.reports.understood")[0]
    assert len(quarantined["payload"]["original_envelope"]["payload"]["embedding"]) == 512


def test_sentinel_never_emits_a_business_topic(memory_bus: InMemoryBus) -> None:
    """Sentinel writes verdicts and quarantine copies. Nothing else."""
    sentinel = Sentinel(memory_bus, persist=False, topics=("reports.understood",))
    _publish(memory_bus, _broken(), "reports.understood")
    _publish(memory_bus, envelope_for("reports.understood"), "reports.understood")
    sentinel.run_once()

    emitted = set(memory_bus.topics()) - {"reports.understood"}

    assert emitted == {"verification.results", "quarantine.reports.understood"}


def test_sentinel_acks_everything_it_reads(sentinel: Sentinel, memory_bus: InMemoryBus) -> None:
    """An un-acked verdict would be re-verified forever."""
    from agents.av_sentinel.agent import GROUP

    _publish(memory_bus, envelope_for(TOPIC))
    sentinel.run_once()

    assert memory_bus.pending(TOPIC, GROUP) == []
    assert sentinel.run_once() == []


def test_mode_defaults_from_configuration(memory_bus: InMemoryBus) -> None:
    assert Sentinel(memory_bus, persist=False).mode in {"strict", "permissive", "shadow"}


# -- regression ----------------------------------------------------------


def test_huge_offending_value_still_produces_an_emittable_verdict(
    memory_bus: InMemoryBus,
) -> None:
    """Regression: jsonschema quotes the offending value in full, so a wrong-length
    768-float embedding produced a multi-kilobyte reason. That overflowed the
    envelope's 2000-character rationale limit and crashed Sentinel on exactly the
    malformed input it exists to catch. Reasons are now capped at the source."""
    sentinel = Sentinel(memory_bus, persist=False, topics=("reports.understood",))
    envelope = envelope_for("reports.understood")
    envelope["payload"]["embedding"] = [0.123456789] * 4096
    _publish(memory_bus, envelope, "reports.understood")

    verdicts = sentinel.run_once()

    assert [v.verdict for v in verdicts] == ["fail_hard"]
    verdict_envelope = memory_bus.messages("verification.results")[0]
    assert len(verdict_envelope["rationale"]) <= 2000
    assert verify_structural(verdict_envelope).passed


def test_reason_messages_are_capped(memory_bus: InMemoryBus) -> None:
    from agents.av_sentinel.layers.structural import MAX_REASON_LENGTH

    envelope = envelope_for("reports.understood")
    envelope["payload"]["embedding"] = [0.123456789] * 4096

    verdict = verify_structural(envelope)

    assert verdict.reasons
    assert all(len(r.message) <= MAX_REASON_LENGTH for r in verdict.reasons)
