"""The topic registry against PRD section 9.3."""

from __future__ import annotations

import pytest

from common.topics import (
    PRODUCERS,
    TOPICS,
    is_known_topic,
    quarantine_topic_for,
    schema_family_for,
    skipped_topic_for,
)

#: Transcribed from the PRD section 9.3 table. If the PRD changes, this list is
#: what should change first.
PRD_TOPICS = {
    "reports.ingested",
    "reports.rejected",
    "reports.understood",
    "reports.linked",
    "incidents.updated",
    "incidents.prioritized",
    "incidents.routed",
    "incidents.unrouted",
    "evidence.attached",
    "resolution.claimed",
    "resolution.verified",
    "resolution.disputed",
    "verification.results",
    "sentinel.alert",
    "deadletter",
}


def test_registry_matches_the_prd_topic_table() -> None:
    assert TOPICS == PRD_TOPICS


@pytest.mark.parametrize(
    ("topic", "producer"),
    [
        ("reports.ingested", "A0"),
        ("reports.understood", "A1"),
        ("reports.linked", "A2"),
        ("incidents.updated", "A3"),
        ("incidents.prioritized", "A4"),
        ("incidents.routed", "A5"),
        ("resolution.verified", "A6"),
        ("evidence.attached", "A7"),
        ("verification.results", "AV"),
        ("resolution.claimed", "dashboard"),
    ],
)
def test_producer_assignments_match_the_prd(topic: str, producer: str) -> None:
    assert PRODUCERS[topic] == producer


def test_concrete_topics_resolve_to_their_own_schema() -> None:
    assert schema_family_for("reports.ingested") == "reports.ingested"


@pytest.mark.parametrize(
    ("topic", "family"),
    [
        ("reports.understood.skipped", "skipped"),
        ("incidents.routed.skipped", "skipped"),
        ("quarantine.reports.ingested", "quarantine"),
        ("quarantine.incidents.prioritized", "quarantine"),
        ("control.a1_perception", "control"),
        ("control.av_sentinel", "control"),
    ],
)
def test_patterned_families_resolve(topic: str, family: str) -> None:
    assert schema_family_for(topic) == family


@pytest.mark.parametrize(
    "topic", ["reports.imaginary", "", ".skipped", "quarantine.", "control.", "nonsense"]
)
def test_unknown_topics_do_not_resolve(topic: str) -> None:
    """A bare family prefix is not a topic; letting it resolve would hand a
    malformed name a valid schema."""
    assert schema_family_for(topic) is None
    assert not is_known_topic(topic)


def test_every_registered_topic_is_known() -> None:
    assert all(is_known_topic(topic) for topic in TOPICS)


def test_quarantine_topic_construction() -> None:
    assert quarantine_topic_for("reports.understood") == "quarantine.reports.understood"
    assert is_known_topic(quarantine_topic_for("reports.understood"))


def test_skipped_topic_construction() -> None:
    assert skipped_topic_for("reports.understood") == "reports.understood.skipped"
    assert is_known_topic(skipped_topic_for("reports.understood"))
