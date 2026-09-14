"""Echo agent - the M0 pipeline smoke test (PRD section 13, M0).

M0's deliverable is "one echo agent", and its acceptance criterion is that an
event flows intake -> echo -> Sentinel L1 and shows up in the trace viewer.
This agent exists to prove that path end to end and nothing more.

It deliberately emits a `*.skipped` event rather than a business event. Echo
has no perception, no dedup and no judgement, so any business output it
produced would be fabricated - and PRD section 7 already provides the honest
way for an agent to say "I consumed this and produced nothing": a skip with a
reason. Real extraction arrives with A1 in P2.

What it does prove, which is the whole point of M0:

  * a message published to reports.ingested is delivered through the bus
  * the envelope parses and its trace_id and causation_id propagate
  * the emitted event is archived to `messages` and the run to `agent_runs`
  * Sentinel verifies the result and records a verdict
  * GET /v1/trace/{trace_id} renders the resulting two-node DAG
"""

from __future__ import annotations

from agents.base import Agent, SkipSignal
from common.envelope import Envelope

__all__ = ["EchoAgent"]


class EchoAgent(Agent):
    """Consumes reports.ingested and emits a skip carrying the same trace."""

    name = "ECHO"
    version = "1.0.0"
    input_topic = "reports.ingested"

    def handle(self, envelope: Envelope) -> list[Envelope]:
        report_id = envelope.payload.get("report_id", "unknown")
        raise SkipSignal(
            reason_code="echo_agent_no_business_logic",
            reason=(
                f"Echo agent saw report {report_id} on {self.input_topic} and has no "
                f"business logic to apply. Perception lands with A1 in P2."
            ),
        )
