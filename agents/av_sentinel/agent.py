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

L1 and L2 run as of P4. The L3 judge and L4 continuous checks are P6; until
then Sentinel reports what it can actually verify rather than implying
broader coverage.

## L2 and `fail_soft` (P4)

L2 (`agents/av_sentinel/layers/invariants.py`) runs after L1 passes, for the
seven topics that have invariants defined (`layers.invariants.has_rules_for`).
A topic with no L2 rules gets only its L1 verdict — no rubber-stamp second
"pass" is published for it.

`fail_soft` is distinct from the retry budget `agents.base.Agent` already
runs. That budget is for *transport or handler* failures — an exception
`handle()` raised. `fail_soft` is a *verification* failure: the handler ran
fine and produced a plausible-looking output that Sentinel's business rules
say is wrong in a way worth one more attempt at (a summary over the word cap,
for instance). Sentinel triggers this retry itself, by clearing the
producing agent's idempotency record for the message that *caused* the failed
output and republishing that cause message to the producer's own input topic,
with the failure reason appended to its `rationale`. One retry only — a
second `fail_soft` on the same message escalates to `fail_hard`/quarantine
rather than retrying forever, tracked in `sentinel_fail_soft_retries`
(migration 0004).
"""

from __future__ import annotations

import time
from typing import Any

from agents.av_sentinel.layers.invariants import InvariantVerdict, has_rules_for, verify_invariants
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

#: Both verdict classes carry the same verdict/reasons/layer shape (L1's
#: `Reason` is reused by L2 deliberately, see `layers/invariants.py`).
AnyVerdict = StructuralVerdict | InvariantVerdict

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

    def run_once(self, *, count: int = 50) -> list[AnyVerdict]:
        """Sweep every topic once. Returns the final verdict reached per message.

        "Final" means: L1's, if L1 failed (L2 cannot reason about an envelope
        that does not match its own contract); L2's otherwise, for topics that
        have L2 rules (`layers.invariants.has_rules_for`); L1's `pass`
        otherwise. Both L1 and L2 verdicts are still recorded and published —
        this return value is what a caller checks to decide "did this message
        clear Sentinel", not the complete record.
        """
        verdicts: list[AnyVerdict] = []
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
            extra={"mode": self.mode, "topics": len(self.topics), "layers": ["L1", "L2"]},
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

    def verify(self, topic: str, raw: dict[str, Any]) -> AnyVerdict:
        """Run L1, then L2 if L1 passed and the topic has rules, and act."""
        trace_id = str(raw.get("trace_id") or uuid7())

        with bind_trace(trace_id):
            l1 = verify_structural(raw)
            l1_id = uuid7()
            self._record_verdict(l1_id, l1, topic, raw, trace_id)
            self._publish_verdict(l1_id, l1, topic, raw, trace_id)

            if l1.verdict == "fail_hard":
                self._quarantine(l1_id, l1, topic, raw, trace_id)
                return l1

            if not has_rules_for(topic):
                return l1

            l2 = verify_invariants(topic, raw, allow_db=self.persist)
            l2_id = uuid7()
            self._record_verdict(l2_id, l2, topic, raw, trace_id)
            self._publish_verdict(l2_id, l2, topic, raw, trace_id)

            if l2.verdict == "fail_hard":
                self._quarantine(l2_id, l2, topic, raw, trace_id)
            elif l2.verdict == "fail_soft":
                self._fail_soft_retry(l2_id, l2, topic, raw, trace_id)

            return l2

    def _record_verdict(
        self,
        verdict_id: Any,
        verdict: AnyVerdict,
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
        verdict: AnyVerdict,
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
        summary = f"{verdict.layer} {verdict.verdict} for {topic}"
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
        verdict: AnyVerdict,
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
            rationale=f"{verdict.layer} {verdict.verdict} on {topic}; quarantined for human triage",
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

    # -- fail_soft (P4, PRD section 8.2) -----------------------------------

    def _fail_soft_retry(
        self,
        verdict_id: Any,
        verdict: AnyVerdict,
        topic: str,
        raw: dict[str, Any],
        trace_id: str,
    ) -> None:
        """One retry of the producer, distinct from `Agent`'s transport retries.

        See the module docstring's "L2 and fail_soft" section for why this
        exists as a separate path. Requires persistence: without a database
        there is no idempotency record to clear and no archived cause message
        to replay, so a `fail_soft` verdict with persistence off is recorded
        and published like any other verdict but nothing is retried — logged,
        not silently dropped.
        """
        if not self.persist:
            log.warning(
                "fail_soft verdict but persistence is off; cannot retry the producer",
                extra={"topic": topic, "message_id": raw.get("message_id")},
            )
            return

        message_id = str(raw.get("message_id") or "")
        causation_id = raw.get("causation_id")
        producer_agent = _producing_agent(raw)
        if not message_id or not causation_id:
            log.warning(
                "fail_soft verdict has no causation_id to retry against",
                extra={"topic": topic, "message_id": message_id},
            )
            return

        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sentinel_fail_soft_retries WHERE message_id = %s", (message_id,)
            )
            already_retried = cur.fetchone() is not None

            cur.execute("SELECT envelope FROM messages WHERE message_id = %s", (str(causation_id),))
            cause_row = cur.fetchone()

            if not already_retried and cause_row is not None:
                cause_envelope = cause_row["envelope"]
                cur.execute(
                    "DELETE FROM handler_results WHERE agent = %s AND handler_key = %s",
                    (producer_agent, _idempotency_key(cause_envelope)),
                )
                cur.execute(
                    """
                    INSERT INTO sentinel_fail_soft_retries
                        (message_id, trace_id, topic, agent, reason, retried_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (message_id) DO NOTHING
                    """,
                    (
                        message_id,
                        trace_id,
                        topic,
                        producer_agent,
                        "; ".join(r.message for r in verdict.reasons)[:MAX_RATIONALE_LENGTH],
                    ),
                )

        if already_retried:
            log.warning(
                "fail_soft verdict on a message already retried once; escalating to quarantine",
                extra={"topic": topic, "message_id": message_id},
            )
            self._quarantine(verdict_id, verdict, topic, raw, trace_id)
            return

        if cause_row is None:
            log.warning(
                "fail_soft verdict but the cause message is not archived; cannot retry",
                extra={"topic": topic, "message_id": message_id, "causation_id": causation_id},
            )
            return

        reason_text = "; ".join(r.message for r in verdict.reasons) or "L2 invariant failed"
        retried = dict(cause_row["envelope"])
        retried["rationale"] = _append_reason(retried.get("rationale", ""), reason_text)

        log.warning(
            "fail_soft: retrying producer",
            extra={"agent": producer_agent, "topic": retried.get("topic"), "reason": reason_text},
        )
        self.bus.publish(str(retried["topic"]), retried)

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


def _idempotency_key(envelope: dict[str, Any]) -> str:
    """`Envelope.idempotency_key()`, recomputed from a raw dict.

    Used only to clear the producer's `handler_results` row for a `fail_soft`
    retry; a raw dict is what `messages.envelope` gives back, and building a
    full `Envelope` just to read four fields would risk the retry itself
    failing on exactly the kind of malformed input Sentinel exists to catch.
    """
    producer = envelope.get("producer") or {}
    version = producer.get("version") if isinstance(producer, dict) else None
    return "|".join(
        (
            str(envelope.get("correlation_id", "")),
            str(envelope.get("topic", "")),
            str(envelope.get("schema_version", "")),
            str(version or ""),
        )
    )


#: Cap the retried rationale so an appended reason never overflows the
#: envelope's 2000-character limit (PRD section 9.2).
_MAX_RATIONALE = 2000


def _append_reason(rationale: str, reason: str) -> str:
    """`rationale`, with the fail_soft reason appended (PRD section 8.2)."""
    suffix = f" [sentinel fail_soft retry: {reason}]"
    if len(rationale) + len(suffix) <= _MAX_RATIONALE:
        return rationale + suffix
    return (rationale + suffix)[: _MAX_RATIONALE - 3] + "..."
