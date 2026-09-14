"""The message archive.

Every envelope published is recorded here before it reaches the bus. Redis
streams are trimmable and PRD section 6.1 puts all durable truth in Postgres,
so this table — not the stream — is what `/v1/trace/{trace_id}` reads and what
makes a trace replayable long after the stream has rolled over.

Archive-before-publish is the ordering, always: a message a consumer can see
must already be in the audit trail, never the other way round.

Whoever publishes, archives. That includes the development harness in
`scripts/emit_report.py`, because a message that entered the system without an
archive row would leave a hole in a trace.
"""

from __future__ import annotations

from common.db import Json, transaction
from common.envelope import Envelope

__all__ = ["archive"]

_INSERT = """
INSERT INTO messages (
    message_id, trace_id, causation_id, correlation_id, topic,
    schema_version, emitted_at, producer_agent, producer_version,
    producer_model, confidence, rationale, envelope
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (message_id) DO NOTHING
"""


def archive(envelope: Envelope) -> None:
    """Record `envelope` in the archive.

    Idempotent: re-archiving the same message_id is a no-op, so a retry that
    re-publishes cannot corrupt the trail.
    """
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            _INSERT,
            (
                str(envelope.message_id),
                str(envelope.trace_id),
                str(envelope.causation_id) if envelope.causation_id else None,
                envelope.correlation_id,
                envelope.topic,
                envelope.schema_version,
                envelope.emitted_at,
                envelope.producer.agent,
                envelope.producer.version,
                envelope.producer.model,
                envelope.confidence,
                envelope.rationale,
                Json(envelope.to_dict()).dumps(),
            ),
        )
