"""The message envelope (PRD section 9.2).

Every message on the bus carries this envelope. Two rules matter most and are
enforced by the constructors below rather than left to callers:

* `trace_id` is minted once by A0 and propagated unchanged downstream, so any
  incident can be replayed back to the original citizen submission.
* `causation_id` points at the message that caused this one, which is what
  lets the trace viewer rebuild the exact decision DAG.

Use `Envelope.originate()` at a trace boundary (A0, the dashboard) and
`parent.derive()` everywhere else. Constructing an Envelope by hand is allowed
but skips that propagation, so tests do it and agents should not.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from common.ids import new_message_id, new_trace_id

__all__ = [
    "SCHEMA_VERSION",
    "Envelope",
    "Producer",
    "Verification",
    "VerificationLayer",
    "VerificationStatus",
    "utcnow",
]

#: Current contract version. Additive payload changes keep the major version;
#: breaking changes bump it and run both consumers side by side (PRD 9.2).
SCHEMA_VERSION = "1.0.0"

VerificationStatus = Literal["pending", "pass", "warn", "fail_soft", "fail_hard"]
VerificationLayer = Literal["L1", "L2", "L3", "L4"]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


def utcnow() -> dt.datetime:
    """Timezone-aware UTC now. Naive datetimes are a bug in this codebase."""
    return dt.datetime.now(dt.UTC)


class Producer(BaseModel):
    """Who emitted this message, and with what."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(min_length=1)
    version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    model: str | None = None


class Verification(BaseModel):
    """Sentinel's verdict slot. Producers leave this at its default."""

    model_config = ConfigDict(extra="forbid")

    status: VerificationStatus = "pending"
    verdict_id: UUID | None = None
    checked_layers: list[VerificationLayer] = Field(default_factory=list)


class Envelope(BaseModel):
    """A single message on the bus."""

    model_config = ConfigDict(extra="forbid")

    message_id: UUID = Field(default_factory=new_message_id)
    trace_id: UUID
    causation_id: UUID | None = None
    correlation_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    schema_version: str = SCHEMA_VERSION
    emitted_at: dt.datetime = Field(default_factory=utcnow)
    producer: Producer
    confidence: Confidence
    rationale: str = Field(min_length=1, max_length=2000)
    verification: Verification = Field(default_factory=Verification)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_serializer("emitted_at")
    def _serialize_emitted_at(self, value: dt.datetime) -> str:
        """RFC 3339 with a trailing Z, matching the PRD's envelope example."""
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def originate(
        cls,
        *,
        topic: str,
        correlation_id: str,
        producer: Producer,
        confidence: float,
        rationale: str,
        payload: dict[str, Any],
        schema_version: str = SCHEMA_VERSION,
    ) -> Self:
        """Start a new trace.

        Only A0 (citizen intake) and the dashboard originate traces. Everything
        else derives from the message it is reacting to.
        """
        return cls(
            trace_id=new_trace_id(),
            causation_id=None,
            correlation_id=correlation_id,
            topic=topic,
            schema_version=schema_version,
            producer=producer,
            confidence=confidence,
            rationale=rationale,
            payload=payload,
        )

    def derive(
        self,
        *,
        topic: str,
        producer: Producer,
        confidence: float,
        rationale: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        schema_version: str = SCHEMA_VERSION,
    ) -> Envelope:
        """Emit a downstream message caused by this one.

        Carries `trace_id` through unchanged and sets `causation_id` to this
        message. `correlation_id` defaults to this message's, which is right
        whenever the subject has not changed; A3 overrides it when a report_id
        becomes an incident_id.
        """
        return Envelope(
            trace_id=self.trace_id,
            causation_id=self.message_id,
            correlation_id=correlation_id if correlation_id is not None else self.correlation_id,
            topic=topic,
            schema_version=schema_version,
            producer=producer,
            confidence=confidence,
            rationale=rationale,
            payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict, which is the form the bus and Sentinel work with."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Rebuild an Envelope from its wire form."""
        return cls.model_validate(data)

    def idempotency_key(self) -> str:
        """Handler dedup key from PRD section 9.4.

        At-least-once delivery means a handler can see the same event twice; a
        repeat keyed identically must be a no-op returning the prior result.
        """
        return "|".join(
            (self.correlation_id, self.topic, self.schema_version, self.producer.version)
        )
