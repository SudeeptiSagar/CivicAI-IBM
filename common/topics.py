"""Topic registry.

Single source of truth for the topic names in PRD section 9.3, who produces
them, and which JSON Schema family validates them.

Three topic families are patterned rather than enumerated:

    <topic>.skipped     an agent consumed an event and produced no business
                        output (PRD section 7, agent contract)
    quarantine.<topic>  Sentinel blocked an envelope (PRD section 8.2)
    control.<agent>     an operational command to one agent (PRD section 9.3)
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "CONTROL_PREFIX",
    "PRODUCERS",
    "QUARANTINE_PREFIX",
    "SKIPPED_SUFFIX",
    "TOPICS",
    "is_known_topic",
    "quarantine_topic_for",
    "schema_family_for",
    "skipped_topic_for",
]

SKIPPED_SUFFIX: Final = ".skipped"
QUARANTINE_PREFIX: Final = "quarantine."
CONTROL_PREFIX: Final = "control."

#: Concrete topics, mapped to the agent that produces them (PRD section 9.3).
PRODUCERS: Final[dict[str, str]] = {
    "reports.ingested": "A0",
    "reports.rejected": "A0",
    "reports.understood": "A1",
    "reports.linked": "A2",
    "incidents.updated": "A3",
    "incidents.prioritized": "A4",
    "incidents.routed": "A5",
    "incidents.unrouted": "A5",
    "evidence.attached": "A7",
    "resolution.claimed": "dashboard",
    "resolution.verified": "A6",
    "resolution.disputed": "A6",
    "verification.results": "AV",
    "sentinel.alert": "AV",
    "deadletter": "any",
}

TOPICS: Final[frozenset[str]] = frozenset(PRODUCERS)


def is_known_topic(topic: str) -> bool:
    """True if `topic` is a concrete topic or a member of a patterned family."""
    return schema_family_for(topic) is not None


def schema_family_for(topic: str) -> str | None:
    """Return the schema basename that validates `topic`, or None if unknown.

    Concrete topics validate against their own schema. Patterned families share
    one schema, because their payload shape does not vary by member.
    """
    if topic in TOPICS:
        return topic
    if topic.endswith(SKIPPED_SUFFIX) and len(topic) > len(SKIPPED_SUFFIX):
        return "skipped"
    if topic.startswith(QUARANTINE_PREFIX) and len(topic) > len(QUARANTINE_PREFIX):
        return "quarantine"
    if topic.startswith(CONTROL_PREFIX) and len(topic) > len(CONTROL_PREFIX):
        return "control"
    return None


def quarantine_topic_for(topic: str) -> str:
    """The quarantine topic a fail_hard envelope on `topic` is diverted to."""
    return f"{QUARANTINE_PREFIX}{topic}"


def skipped_topic_for(topic: str) -> str:
    """The skipped topic an agent emits instead of `topic` when it has no output."""
    return f"{topic}{SKIPPED_SUFFIX}"
