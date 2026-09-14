"""AV Sentinel - the verification agent (PRD section 8).

Sentinel does not subclass `Agent`, and the difference is the point. Every
other agent consumes one topic and emits business events; Sentinel subscribes
to *every* topic and emits only verdicts. Forcing it through the same base
class would mean weakening that base class for one special case.

Two rules from PRD section 8.2 are enforced structurally here:

* **Sentinel cannot mutate business data.** It writes to `verification_results`,
  `quarantine` and `sentinel_alerts` and nothing else. There is no code path in
  this module that touches reports, incidents or resolutions. (It archives its
  own emitted envelopes to `messages` like every agent — that table is the
  audit trail, not business data.)
* **It can flag and block, but never fix.** A `fail_hard` envelope is copied to
  quarantine and the original is never repaired or re-emitted.

Self-verification is excluded deliberately. Sentinel's own outputs
(`verification.results`, `sentinel.alert`) would otherwise produce a verdict
for every verdict, forever. PRD section 8.2 covers Sentinel with a separate
meta-check instead, which arrives with L4 in P6.

Only L1 runs in P1. L2 invariants, the L3 judge and L4 continuous checks are
P4 and P6; until then Sentinel reports what it can actually verify rather than
implying broader coverage.
"""

from __future__ import annotations

import time
from typing import Any

from agents.av_sentinel.layers.structural import StructuralVerdict, verify_structural
from bus.base import Bus
from common.config import SentinelMode, settings
from common.db import Json, transaction
from common.envelope import Envelope, Producer, utcnow
from common.ids import uuid7
from common.logging import bind_trace, get_logger
from common.messagelog import archive
from common.topics import TOPICS, quarantine_topic_for, skipped_topic_for

__all__ = ["SELF_TOPICS", "VERIFIED_TOPICS", "Sentinel"]

log = get_logger(__name__)

#: Sentinel's own outputs. Verifying these would recurse without end.
#: quarantine.* is also Sentinel's, and is excluded by not being in TOPICS.
SELF_TOPICS = frozenset({"verification.results", "sentinel.alert"})

_WATCHED = TOPICS - SELF_TOPICS

#: Every topic Sentinel checks.
#:
#: PRD section 8 says "subscribes to EVERY topic", which includes the skip
#: family: an agent that emits `<topic>.skipped` has made a decision, and an
#: unverified decision is exactly what Sentinel exists to prevent. Skip topics
#: are patterned rather than enumerated (PRD section 7), so they are derived
#: here from the concrete topics.
#:
#: This is deliberately liberal. A skip stream that never receives a message
#: costs an idle consumer group; a topic Sentinel forgot to watch costs an
#: unverified event reaching a dashboard.
VERIFIED_TOPICS: tuple[str, ...] = tuple(
    sorted(_WATCHED | {skipped_topic_for(topic) for topic in _WATCHED})
)

VERDICT_TOPIC = "verification.results"
GROUP = "av_sentinel"

#: The envelope contract caps rationale at 2000 characters (PRD section 9.2).
MAX_RATIONALE_LENGTH = 2000


class Sentinel:
    """Verifies every message on every topic."""

    name = "AV"
    version = "1.0.0"

    def __init__(
        self,
        bus: Bus,
        *,
        consumer: str = "sentinel-1",
        mode: SentinelMode | None = None,
        persist: bool = True,
        topics: tuple[str, ...] = VERIFIED_TOPICS,
    ) -> None:
        self.bus = bus
        self.consumer = consumer
        self.mode: SentinelMode = mode or settings().sentinel_mode
        self.persist = persist
        self.topics = topics
        self._running = False

    @property
    def producer(self) -> Producer:
        return Producer(agent=self.name, version=self.version, model=None)

    # -- the loop ---------------------------------------------------------

    def subscribe_all(self) -> None:
        """Create Sentinel's consumer group on every verified topic."""
        for topic in self.topics:
            self.bus.create_group(topic, GROUP)

    def run_once(self, *, count: int = 50) -> list[StructuralVerdict]:
        """Sweep every topic once. Returns the verdicts reached."""
        verdicts: list[StructuralVerdict] = []
        for topic in self.topics:
            self.bus.create_group(topic, GROUP)
            for message in self.bus.subscribe(topic, GROUP, self.consumer, count=count):
                verdicts.append(self.verify(topic, message.envelope))
                self.bus.ack(topic, GROUP, message.message_id)
        return verdicts

    def run_forever(self, *, idle_sleep: float = 0.5) -> None:
        """Sweep until stopped. The container entrypoint."""
        self._running = True
        self.subscribe_all()
        log.info(
            "sentinel starting",
            extra={"mode": self.mode, "topics": len(self.topics), "layers": ["L1"]},
        )
        while self._running:
            try:
                if not self.run_once():
                    time.sleep(idle_sleep)
            except Exception:
                log.exception("sentinel loop error")
                time.sleep(1.0)

    def stop(self) -> None:
        self._running = False

    # -- verification -----------------------------------------------------

    def verify(self, topic: str, raw: dict[str, Any]) -> StructuralVerdict:
        """Run L1 on one envelope and act on the verdict."""
        trace_id = str(raw.get("trace_id") or uuid7())

        with bind_trace(trace_id):
            verdict = verify_structural(raw)
            verdict_id = uuid7()

            self._record_verdict(verdict_id, verdict, topic, raw, trace_id)
            self._publish_verdict(verdict_id, verdict, topic, raw, trace_id)

            if verdict.verdict == "fail_hard":
                self._quarantine(verdict_id, verdict, topic, raw, trace_id)

            return verdict

    def _record_verdict(
        self,
        verdict_id: Any,
        verdict: StructuralVerdict,
        topic: str,
        raw: dict[str, Any],
        trace_id: str,
    ) -> None:
        if not self.persist:
            return
        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO verification_results (
                    verdict_id, message_id, trace_id, topic, agent, layer,
                    verdict, reasons, judge_model
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)
                ON CONFLICT (message_id, layer) DO NOTHING
                """,
                (
                    str(verdict_id),
                    str(raw.get("message_id") or uuid7()),
                    trace_id,
                    topic,
                    _producing_agent(raw),
                    verdict.layer,
                    verdict.verdict,
                    Json([r.to_dict() for r in verdict.reasons]).dumps(),
                ),
            )

    def _publish_verdict(
        self,
        verdict_id: Any,
        verdict: StructuralVerdict,
        topic: str,
        raw: dict[str, Any],
        trace_id: str,
    ) -> None:
        payload = {
            "verdict_id": str(verdict_id),
            "message_id": str(raw.get("message_id") or uuid7()),
            "trace_id": trace_id,
            "topic": topic,
            "agent": _producing_agent(raw),
            "layer": verdict.layer,
            "verdict": verdict.verdict,
            "reasons": [r.to_dict() for r in verdict.reasons],
            "judge_model": None,
            "created_at": utcnow().isoformat().replace("+00:00", "Z"),
        }
        summary = f"L1 {verdict.verdict} for {topic}"
        if verdict.reasons:
            summary += f": {verdict.reasons[0].message}"

        envelope = self._envelope_for(
            raw,
            topic=VERDICT_TOPIC,
            # Belt and braces: reasons are already capped at the source, but a
            # verdict that cannot be emitted is worse than a terse one.
            rationale=summary[:MAX_RATIONALE_LENGTH],
            payload=payload,
            trace_id=trace_id,
        )
        self._emit(envelope)

    def _quarantine(
        self,
        verdict_id: Any,
        verdict: StructuralVerdict,
        topic: str,
        raw: dict[str, Any],
        trace_id: str,
    ) -> None:
        """Divert a failed envelope so no downstream consumer ever sees it."""
        reasons = [r.to_dict() for r in verdict.reasons]
        quarantined_at = utcnow().isoformat().replace("+00:00", "Z")

        if self.persist:
            with transaction() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quarantine (
                        quarantine_id, message_id, trace_id, topic, verdict_id,
                        envelope, reasons
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid7()),
                        str(raw.get("message_id") or uuid7()),
                        trace_id,
                        topic,
                        str(verdict_id),
                        Json(raw).dumps(),
                        Json(reasons).dumps(),
                    ),
                )

        envelope = self._envelope_for(
            raw,
            topic=quarantine_topic_for(topic),
            rationale=f"L1 fail_hard on {topic}; quarantined for human triage",
            payload={
                "original_envelope": raw,
                "verdict_id": str(verdict_id),
                "quarantined_at": quarantined_at,
                "reasons": reasons,
            },
            trace_id=trace_id,
        )
        log.warning(
            "quarantined message",
            extra={"topic": topic, "reasons": [r["code"] for r in reasons]},
        )
        self._emit(envelope)

    # -- helpers ----------------------------------------------------------

    def _envelope_for(
        self,
        raw: dict[str, Any],
        *,
        topic: str,
        rationale: str,
        payload: dict[str, Any],
        trace_id: str,
    ) -> Envelope:
        """Build a Sentinel output that keeps the subject's trace.

        The inspected envelope may itself be malformed — that is the whole
        reason Sentinel exists — so this falls back to originating a message
        carrying the salvaged trace_id rather than failing to report.
        """
        try:
            parent = Envelope.from_dict(raw)
        except Exception:
            envelope = Envelope.originate(
                topic=topic,
                correlation_id=str(raw.get("correlation_id") or trace_id),
                producer=self.producer,
                confidence=1.0,
                rationale=rationale,
                payload=payload,
            )
            return envelope.model_copy(update={"trace_id": _as_uuid(trace_id)})

        return parent.derive(
            topic=topic,
            producer=self.producer,
            confidence=1.0,
            rationale=rationale,
            payload=payload,
        )

    def _emit(self, envelope: Envelope) -> None:
        """Archive then publish, matching the ordering every agent uses."""
        if self.persist:
            archive(envelope)
        self.bus.publish(envelope.topic, envelope.to_dict())


def _producing_agent(raw: dict[str, Any]) -> str:
    producer = raw.get("producer")
    if isinstance(producer, dict):
        agent = producer.get("agent")
        if isinstance(agent, str) and agent:
            return agent
    return "unknown"


def _as_uuid(value: str) -> Any:
    from uuid import UUID

    try:
        return UUID(value)
    except ValueError:
        return uuid7()
