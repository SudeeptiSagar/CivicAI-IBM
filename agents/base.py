"""The agent runtime.

PRD section 7 states one contract that every agent obeys. Rather than restate
it in eight places, it is implemented once here and subclasses fill in only
`handle()`:

* consumes from exactly one input topic (plus `control.*`)
* idempotent on correlation_id + schema_version (PRD section 9.4)
* emits exactly one output event per input, or a `*.skipped` event with a reason
* attaches confidence and a machine-readable rationale
* never writes to another agent's table
* fails loudly to `deadletter` rather than guessing

Retry policy (PRD section 9.4) is in-process: three attempts with 1s/4s/16s
backoff, then the message is deadlettered and acked. Acking a poison pill is
deliberate — re-queueing it forever is exactly what the PRD's poison-pill
protection forbids. Bus redelivery is reserved for the other failure mode, a
process that dies mid-handler: it never acked, so the entry stays pending and
another consumer reclaims it.

## The verdict gate (P4, PRD section 8.3)

Off by default (`Settings.sentinel_gate_enabled`). When on, and only when
`persist=True` (the gate reads `verification_results`, which needs a
database — see `common/verdict_gate.py`), every delivery is checked against
Sentinel's verdict for it *before* `handle()` runs:

* `pass`/`warn` within the deadline, or the deadline lapses in `permissive`
  mode: `handle()` runs as normal.
* `fail_soft`/`fail_hard` found within the deadline: this delivery is acked
  without calling `handle()` — Sentinel has already reacted to it (a
  producer retry or quarantine), so nothing here should also act on it.
* `strict` mode, deadline lapses with no verdict yet: the delivery is
  **nacked**, not acked — deferred for the next sweep rather than treated as
  a failure of `handle()`, since `handle()` never ran. It still counts
  toward the poison-pill budget on redelivery, so a message Sentinel never
  verifies eventually deadletters instead of waiting forever.

This is orthogonal to the retry budget below: the backoff loop retries a
`handle()` that raised; the gate defers a `handle()` that has not been
allowed to run yet.
"""

from __future__ import annotations

import abc
import time
import traceback
from dataclasses import dataclass
from typing import Any

from bus.base import MAX_DELIVERIES, Bus, BusMessage
from common.config import settings
from common.db import Json, transaction
from common.envelope import Envelope, Producer
from common.ids import uuid7
from common.logging import bind_trace, get_logger
from common.messagelog import archive
from common.topics import skipped_topic_for
from common.verdict_gate import GateResult, PostgresVerdictStore, VerdictGate

__all__ = ["BACKOFF_SECONDS", "Agent", "AgentResult", "SkipSignal"]

log = get_logger(__name__)

#: PRD section 9.4: 3 attempts, exponential backoff, then deadletter.
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0, 16.0)

DEADLETTER_TOPIC = "deadletter"


class SkipSignal(Exception):
    """Raised by `handle()` to emit a `*.skipped` event instead of an output.

    A skip is a decision, not a failure: it is acked, recorded, and carries a
    reason downstream. Use it when there is genuinely nothing to produce.
    """

    def __init__(self, reason_code: str, reason: str) -> None:
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AgentResult:
    """What one delivery produced."""

    handled: bool
    emitted: list[Envelope]
    skipped: bool = False
    error: str | None = None


class Agent(abc.ABC):
    """Base class for every CivicAI agent."""

    #: Short agent id from PRD section 7 ("A0", "A1", ... "AV").
    name: str
    #: Semver of this agent build. Part of the idempotency key, so a redeploy
    #: correctly treats the same input as new work.
    version: str
    #: The single topic this agent consumes.
    input_topic: str
    #: The model backing this agent's reasoning, if any.
    model: str | None = None

    def __init__(
        self,
        bus: Bus,
        *,
        consumer: str = "0",
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
        persist: bool = True,
        verdict_gate: VerdictGate | None = None,
    ) -> None:
        """
        Args:
            bus: transport to consume from and publish to.
            consumer: this worker's name within the consumer group.
            backoff: retry delays. Tests pass `(0, 0, 0)` to skip the waiting.
            persist: write agent_runs, messages and idempotency rows. Disabled
                in unit tests that have no database.
            verdict_gate: override the gate used when
                `Settings.sentinel_gate_enabled` is on. Tests inject a gate
                with a fake store and no real sleeping; production takes the
                default, built from `PostgresVerdictStore`. Ignored entirely
                when the flag is off or `persist` is False — see the class
                docstring's "verdict gate" section.
        """
        self.bus = bus
        self.consumer = consumer
        self.backoff = backoff
        self.persist = persist
        self._running = False
        self._verdict_gate = verdict_gate
        if self._verdict_gate is None and persist and settings().sentinel_gate_enabled:
            self._verdict_gate = VerdictGate(PostgresVerdictStore())

    @property
    def gate_enabled(self) -> bool:
        return self._verdict_gate is not None

    # -- what subclasses implement ----------------------------------------

    @abc.abstractmethod
    def handle(self, envelope: Envelope) -> list[Envelope]:
        """Process one input message and return the messages to emit.

        Build outputs with `envelope.derive(...)` so trace_id and causation_id
        propagate. Raise `SkipSignal` to emit a `*.skipped` event instead.
        Any other exception is retried, then deadlettered.
        """

    # -- identity ---------------------------------------------------------

    @property
    def group(self) -> str:
        """Consumer group name. One per agent, so every agent sees every event."""
        return f"{self.name.lower()}_{type(self).__name__.lower()}"

    @property
    def producer(self) -> Producer:
        """This agent's producer block for outgoing envelopes."""
        return Producer(agent=self.name, version=self.version, model=self.model)

    # -- the loop ---------------------------------------------------------

    def run_once(self, *, count: int = 10, block_ms: int = 0) -> list[AgentResult]:
        """Claim and process up to `count` messages. Returns one result each."""
        self.bus.create_group(self.input_topic, self.group)
        messages = self.bus.subscribe(
            self.input_topic, self.group, self.consumer, count=count, block_ms=block_ms
        )
        return [self._process(message) for message in messages]

    def run_forever(self, *, block_ms: int = 5000) -> None:
        """Consume until stopped. The container entrypoint."""
        self._running = True
        log.info(
            "agent starting",
            extra={"agent": self.name, "topic": self.input_topic, "group": self.group},
        )
        while self._running:
            try:
                self.run_once(block_ms=block_ms)
            except Exception:
                # The loop itself must survive a transport blip; individual
                # message failures are already handled inside _process.
                log.exception("agent loop error", extra={"agent": self.name})
                time.sleep(1.0)

    def stop(self) -> None:
        self._running = False

    # -- one message ------------------------------------------------------

    def _process(self, message: BusMessage) -> AgentResult:
        """Handle a single delivery end to end."""
        try:
            envelope = Envelope.from_dict(message.envelope)
        except Exception as exc:
            # An envelope this agent cannot even parse is not retryable. Sentinel
            # should have caught it upstream; deadletter it and move on.
            self._deadletter(message, [f"{type(exc).__name__}: {exc}"])
            self.bus.ack(message.topic, self.group, message.message_id)
            return AgentResult(handled=False, emitted=[], error=str(exc))

        with bind_trace(str(envelope.trace_id)):
            return self._process_envelope(message, envelope)

    def _process_envelope(self, message: BusMessage, envelope: Envelope) -> AgentResult:
        if message.is_poison:
            # Already burned the retry budget across process restarts.
            self._deadletter(message, [f"delivery_count reached {message.delivery_count}"])
            self.bus.ack(message.topic, self.group, message.message_id)
            return AgentResult(handled=False, emitted=[], error="poison pill")

        key = envelope.idempotency_key()
        if self.persist and self._already_handled(key):
            log.info("duplicate suppressed", extra={"agent": self.name, "key": key})
            self.bus.ack(message.topic, self.group, message.message_id)
            return AgentResult(handled=False, emitted=[])

        gate = self._verdict_gate
        if gate is not None:
            gated = self._await_gate(gate, message, envelope)
            if gated is not None:
                return gated

        run_id = uuid7()
        started = time.time()
        self._record_run_start(run_id, envelope, message.delivery_count)

        errors: list[str] = []
        for attempt, delay in enumerate(self.backoff, start=1):
            try:
                emitted = self.handle(envelope)
            except SkipSignal as skip:
                emitted = [self._skip_envelope(envelope, skip)]
                self._publish_all(emitted)
                self._record_run_end(run_id, "skipped", None, started)
                self._remember(key, {"skipped": skip.reason_code})
                self.bus.ack(message.topic, self.group, message.message_id)
                return AgentResult(handled=True, emitted=emitted, skipped=True)
            except Exception as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                log.warning(
                    "handler failed",
                    extra={"agent": self.name, "attempt": attempt, "error": str(exc)},
                )
                if attempt < len(self.backoff):
                    time.sleep(delay)
                continue
            else:
                self._publish_all(emitted)
                self._record_run_end(run_id, "ok", None, started)
                self._remember(key, {"emitted": [str(e.message_id) for e in emitted]})
                self.bus.ack(message.topic, self.group, message.message_id)
                return AgentResult(handled=True, emitted=emitted)

        # Retry budget exhausted: fail loudly rather than guess (PRD section 7).
        self._record_run_end(run_id, "error", "; ".join(errors), started)
        self._deadletter(message, errors)
        self.bus.ack(message.topic, self.group, message.message_id)
        return AgentResult(handled=False, emitted=[], error="; ".join(errors))

    # -- the verdict gate (P4, PRD section 8.3) ----------------------------

    def _await_gate(
        self, gate: VerdictGate, message: BusMessage, envelope: Envelope
    ) -> AgentResult | None:
        """Consult the verdict gate. Returns a terminal result, or None to proceed.

        None means `handle()` should run as normal — either the gate found
        `pass`/`warn`, or the deadline lapsed in `permissive` mode.
        """
        result: GateResult = gate.await_verdict(str(envelope.message_id))

        if result.decision == "proceed":
            return None

        if result.decision == "drop":
            # Sentinel already reacted to this specific message (a producer
            # retry for fail_soft, or quarantine for fail_hard). Acting on it
            # here would be exactly the race the gate exists to close.
            log.info(
                "verdict gate dropped delivery",
                extra={"agent": self.name, "verdict": result.verdict, "topic": message.topic},
            )
            self.bus.ack(message.topic, self.group, message.message_id)
            return AgentResult(handled=False, emitted=[], error=None)

        # decision == "defer": strict mode, no verdict within the deadline.
        # Nack rather than ack: handle() never ran, so this is not a failure
        # of it, and a message Sentinel never verifies still needs to reach
        # the poison-pill budget eventually rather than wait forever.
        log.info(
            "verdict gate deferred delivery",
            extra={"agent": self.name, "topic": message.topic, "waited_ms": result.waited_ms},
        )
        self.bus.nack(message.topic, self.group, message.message_id)
        return AgentResult(handled=False, emitted=[], error="verdict_pending")

    # -- emitting ---------------------------------------------------------

    def _skip_envelope(self, envelope: Envelope, skip: SkipSignal) -> Envelope:
        return envelope.derive(
            topic=skipped_topic_for(self.output_topic_for_skip(envelope)),
            producer=self.producer,
            confidence=1.0,
            rationale=skip.reason,
            payload={
                "input_message_id": str(envelope.message_id),
                "reason_code": skip.reason_code,
                "reason": skip.reason,
            },
        )

    def output_topic_for_skip(self, envelope: Envelope) -> str:
        """Topic the skip stands in for. Defaults to this agent's input topic.

        Override when an agent's output topic differs from its input and the
        skip should be readable as "no output on <that> topic".
        """
        return self.input_topic

    def _publish_all(self, envelopes: list[Envelope]) -> None:
        for envelope in envelopes:
            self.emit(envelope)

    def emit(self, envelope: Envelope) -> str:
        """Archive an envelope, then publish it.

        Archive-before-publish is deliberate: a message a consumer can see must
        already be in the audit trail, never the other way round.
        """
        self._archive(envelope)
        return self.bus.publish(envelope.topic, envelope.to_dict())

    def _deadletter(self, message: BusMessage, errors: list[str]) -> None:
        """Publish a terminal failure (PRD section 9.4)."""
        payload = {
            "agent": self.name,
            "attempts": message.delivery_count,
            "error_chain": errors or ["unknown error"],
            "original_envelope": message.envelope,
            "failed_at": _now_iso(),
        }
        envelope = Envelope.originate(
            topic=DEADLETTER_TOPIC,
            correlation_id=str(message.envelope.get("correlation_id", message.message_id)),
            producer=self.producer,
            confidence=1.0,
            rationale=f"{self.name} exhausted its retry budget on {message.topic}",
            payload=payload,
        )
        log.error(
            "deadlettering message",
            extra={"agent": self.name, "topic": message.topic, "errors": errors},
        )
        self.emit(envelope)

    # -- persistence ------------------------------------------------------
    #
    # `messages`, `agent_runs` and `handler_results` are audit and runtime
    # infrastructure, not business data: every agent writes its own rows and
    # none of them belong to another agent.

    def _archive(self, envelope: Envelope) -> None:
        if not self.persist:
            return
        archive(envelope)

    def _record_run_start(self, run_id: Any, envelope: Envelope, delivery_count: int) -> None:
        if not self.persist:
            return
        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_runs (
                    run_id, agent, version, message_id, trace_id, started_at,
                    outcome, delivery_count
                ) VALUES (%s, %s, %s, %s, %s, now(), 'running', %s)
                """,
                (
                    str(run_id),
                    self.name,
                    self.version,
                    str(envelope.message_id),
                    str(envelope.trace_id),
                    delivery_count,
                ),
            )

    def _record_run_end(self, run_id: Any, outcome: str, error: str | None, started: float) -> None:
        if not self.persist:
            return
        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_runs SET ended_at = now(), outcome = %s, error = %s "
                "WHERE run_id = %s",
                (outcome, error, str(run_id)),
            )
        log.info(
            "run complete",
            extra={
                "agent": self.name,
                "outcome": outcome,
                "duration_ms": round((time.time() - started) * 1000, 2),
            },
        )

    def _already_handled(self, key: str) -> bool:
        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM handler_results WHERE agent = %s AND handler_key = %s",
                (self.name, key),
            )
            return cur.fetchone() is not None

    def _remember(self, key: str, result: dict[str, Any]) -> None:
        if not self.persist:
            return
        with transaction() as conn, conn.cursor() as cur:
            # First write wins: a redelivery must not rewrite the original outcome.
            cur.execute(
                "INSERT INTO handler_results (agent, handler_key, result) VALUES (%s, %s, %s) "
                "ON CONFLICT (agent, handler_key) DO NOTHING",
                (self.name, key, Json(result).dumps()),
            )


def _now_iso() -> str:
    from common.envelope import utcnow

    return utcnow().isoformat().replace("+00:00", "Z")


def format_exception(exc: BaseException) -> str:
    """Single-line exception summary for an error chain."""
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()


# Re-exported so agents can reason about the retry budget without importing bus.
MAX_ATTEMPTS = MAX_DELIVERIES
