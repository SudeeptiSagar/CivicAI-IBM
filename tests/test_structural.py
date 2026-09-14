"""Sentinel L1 — structural verification (PRD section 8.1).

Two obligations, and the second matters more than the first:

1. every well-formed message passes
2. every known-bad payload is rejected

The bad-payload table below is the seed of the meta-check from PRD section 8.2:
if Sentinel ever passes a payload it must fail, it has stopped being a
verification layer and become decoration.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.av_sentinel.layers.structural import (
    CODE_MISSING_SCHEMA_VERSION,
    CODE_MISSING_TOPIC,
    CODE_NOT_AN_OBJECT,
    CODE_SCHEMA_VIOLATION,
    CODE_UNKNOWN_CONTRACT,
    verify_structural,
)
from tests.factories import ALL_TOPICS, envelope_for, uid

# -- the happy path ------------------------------------------------------


@pytest.mark.parametrize("topic", ALL_TOPICS)
def test_valid_message_passes(topic: str) -> None:
    verdict = verify_structural(envelope_for(topic))
    assert verdict.passed, verdict.reasons


@pytest.mark.parametrize("topic", ALL_TOPICS)
def test_passing_verdict_carries_no_reasons(topic: str) -> None:
    assert verify_structural(envelope_for(topic)).reasons == []


def test_verdict_layer_is_l1() -> None:
    assert verify_structural(envelope_for("reports.ingested")).layer == "L1"


# -- envelope-level rejection -------------------------------------------


def _mutate(base_topic: str, **changes: Any) -> dict[str, Any]:
    """A valid envelope for `base_topic` with top-level fields overridden."""
    envelope = envelope_for(base_topic)
    envelope.update(changes)
    return envelope


def _payload_mutation(base_topic: str, **changes: Any) -> dict[str, Any]:
    """A valid envelope for `base_topic` with payload fields overridden."""
    envelope = envelope_for(base_topic)
    envelope["payload"].update(changes)
    return envelope


def _drop(base_topic: str, field: str) -> dict[str, Any]:
    """A valid envelope for `base_topic` with one top-level field removed."""
    envelope = envelope_for(base_topic)
    del envelope[field]
    return envelope


def test_non_object_is_rejected() -> None:
    verdict = verify_structural({"not": "an envelope"})
    assert not verdict.passed
    assert verdict.reasons[0].code == CODE_MISSING_TOPIC


def test_missing_topic_is_rejected() -> None:
    verdict = verify_structural(_drop("reports.ingested", "topic"))
    assert verdict.reasons[0].code == CODE_MISSING_TOPIC


def test_missing_schema_version_is_rejected() -> None:
    verdict = verify_structural(_drop("reports.ingested", "schema_version"))
    assert verdict.reasons[0].code == CODE_MISSING_SCHEMA_VERSION


def test_unknown_topic_is_rejected_not_ignored() -> None:
    """A rogue or typo'd topic must never reach a consumer unvalidated."""
    verdict = verify_structural(_mutate("reports.ingested", topic="reports.invented"))
    assert not verdict.passed
    assert verdict.reasons[0].code == CODE_UNKNOWN_CONTRACT


def test_unknown_major_version_is_rejected() -> None:
    verdict = verify_structural(_mutate("reports.ingested", schema_version="9.0.0"))
    assert verdict.reasons[0].code == CODE_UNKNOWN_CONTRACT


def test_topic_mismatch_between_envelope_and_schema_is_rejected() -> None:
    """An A1 payload published under A0's topic is a real failure mode when an
    agent's output wiring is wrong; the topic const catches it."""
    envelope = envelope_for("reports.understood")
    envelope["topic"] = "reports.ingested"
    assert not verify_structural(envelope).passed


BAD_ENVELOPES: list[tuple[str, dict[str, Any]]] = [
    ("confidence above 1", _mutate("reports.ingested", confidence=1.4)),
    ("confidence below 0", _mutate("reports.ingested", confidence=-0.1)),
    ("confidence not a number", _mutate("reports.ingested", confidence="high")),
    ("null trace_id", _mutate("reports.ingested", trace_id=None)),
    ("malformed trace_id", _mutate("reports.ingested", trace_id="not-a-uuid")),
    ("empty correlation_id", _mutate("reports.ingested", correlation_id="")),
    ("empty rationale", _mutate("reports.ingested", rationale="")),
    ("malformed emitted_at", _mutate("reports.ingested", emitted_at="2026-13-45")),
    ("non-semver schema_version", _mutate("reports.ingested", schema_version="1")),
    (
        "unknown verification status",
        _mutate(
            "reports.ingested",
            verification={"status": "probably_fine", "verdict_id": None, "checked_layers": []},
        ),
    ),
    (
        "unknown verification layer",
        _mutate(
            "reports.ingested",
            verification={"status": "pending", "verdict_id": None, "checked_layers": ["L9"]},
        ),
    ),
    ("producer missing version", _mutate("reports.ingested", producer={"agent": "A0"})),
    ("undeclared top-level field", _mutate("reports.ingested", surprise="extra")),
]


@pytest.mark.parametrize("case", BAD_ENVELOPES, ids=[name for name, _ in BAD_ENVELOPES])
def test_bad_envelope_is_rejected(case: tuple[str, dict[str, Any]]) -> None:
    _, envelope = case
    verdict = verify_structural(envelope)
    assert not verdict.passed
    assert verdict.reasons


# -- payload-level rejection --------------------------------------------

BAD_PAYLOADS: list[tuple[str, dict[str, Any]]] = [
    # A1: the embedding dimension invariant the PRD calls out by name.
    ("embedding too short", _payload_mutation("reports.understood", embedding=[0.1] * 512)),
    ("embedding too long", _payload_mutation("reports.understood", embedding=[0.1] * 1024)),
    ("category outside taxonomy", _payload_mutation("reports.understood", category="ufo_sighting")),
    ("severity above range", _payload_mutation("reports.understood", severity_raw=9)),
    ("severity not an integer", _payload_mutation("reports.understood", severity_raw=3.5)),
    ("unknown hazard flag", _payload_mutation("reports.understood", hazard_flags=["dragons"])),
    # A4: a score with no breakdown is exactly what the PRD says to reject.
    ("priority above 100", _payload_mutation("incidents.prioritized", priority_score=140)),
    ("breakdown missing factors", _payload_mutation("incidents.prioritized", factor_breakdown=[])),
    ("empty why", _payload_mutation("incidents.prioritized", why="")),
    ("unknown priority band", _payload_mutation("incidents.prioritized", priority_band="urgent")),
    # A5: department must come from the registry, never a free string.
    ("department off-registry", _payload_mutation("incidents.routed", primary_department="misc")),
    # A2/A3 shape errors.
    ("unknown dedup decision", _payload_mutation("reports.linked", decision="probably")),
    ("match score above 1", _payload_mutation("reports.linked", match_score=1.3)),
    ("zero member reports", _payload_mutation("incidents.updated", member_report_ids=[])),
    ("report count below 1", _payload_mutation("incidents.updated", report_count=0)),
    (
        "duplicate member reports",
        _payload_mutation(
            "incidents.updated", member_report_ids=["00000000-0000-7000-8000-000000000001"] * 2
        ),
    ),
    # A6: a disputed resolution that did not reopen contradicts the PRD.
    ("disputed without reopen", _payload_mutation("resolution.disputed", reopened=False)),
    ("verified with wrong status", _payload_mutation("resolution.verified", final_status="closed")),
    # Undeclared payload fields are rejected, so a renamed field fails loudly
    # instead of silently arriving as None downstream.
    ("undeclared payload field", _payload_mutation("reports.ingested", nonsense=1)),
]


@pytest.mark.parametrize("case", BAD_PAYLOADS, ids=[name for name, _ in BAD_PAYLOADS])
def test_bad_payload_is_rejected(case: tuple[str, dict[str, Any]]) -> None:
    _, envelope = case
    verdict = verify_structural(envelope)
    assert not verdict.passed
    assert all(r.code == CODE_SCHEMA_VIOLATION for r in verdict.reasons)


def test_missing_required_payload_field_is_rejected() -> None:
    envelope = envelope_for("reports.understood")
    del envelope["payload"]["embedding"]
    assert not verify_structural(envelope).passed


def test_reasons_name_the_failing_path() -> None:
    """Quarantine triage is unusable if a verdict cannot say what broke."""
    envelope = _payload_mutation("reports.understood", embedding=[0.1] * 512)
    verdict = verify_structural(envelope)
    assert any(r.path == "payload.embedding" for r in verdict.reasons)


def test_all_failures_are_reported_not_just_the_first() -> None:
    envelope = envelope_for("reports.understood")
    envelope["payload"]["category"] = "ufo_sighting"
    envelope["payload"]["severity_raw"] = 99
    verdict = verify_structural(envelope)
    paths = {r.path for r in verdict.reasons}
    assert {"payload.category", "payload.severity_raw"} <= paths


# -- verdict serialisation ----------------------------------------------


def test_verdict_serialises_for_the_verification_results_topic() -> None:
    """The verdict has to survive the trip onto verification.results."""
    envelope = _payload_mutation("reports.understood", embedding=[0.1] * 512)
    verdict = verify_structural(envelope).to_dict()

    assert verdict["verdict"] == "fail_hard"
    assert verdict["layer"] == "L1"
    assert all({"code", "message", "path"} == set(r) for r in verdict["reasons"])


def test_sentinel_does_not_mutate_the_envelope_it_checks() -> None:
    """PRD section 8.2: Sentinel can flag and block, but never fix."""
    envelope = envelope_for("reports.ingested")
    before = envelope_for("reports.ingested")
    before.update(envelope)
    snapshot = {k: v for k, v in envelope.items()}

    verify_structural(envelope)

    assert envelope == snapshot
    assert envelope["verification"]["status"] == "pending"


def test_family_topics_validate_against_their_family_schema() -> None:
    """Patterned topics resolve by prefix/suffix, not by exact name."""
    assert verify_structural(envelope_for("reports.understood.skipped")).passed
    assert verify_structural(envelope_for("quarantine.reports.understood")).passed
    assert verify_structural(envelope_for("control.a2_dedup")).passed


def test_skipped_family_requires_a_reason() -> None:
    """PRD section 7: a *.skipped event must say why it skipped."""
    envelope = envelope_for("reports.understood.skipped")
    del envelope["payload"]["reason"]
    assert not verify_structural(envelope).passed


def test_quarantine_requires_at_least_one_reason() -> None:
    envelope = envelope_for("quarantine.reports.understood")
    envelope["payload"]["reasons"] = []
    assert not verify_structural(envelope).passed


def test_unknown_control_command_is_rejected() -> None:
    envelope = envelope_for("control.a2_dedup")
    envelope["payload"]["command"] = "self_destruct"
    assert not verify_structural(envelope).passed


def test_empty_family_suffix_is_not_a_valid_topic() -> None:
    """ "quarantine." with nothing after it must not resolve to the family."""
    verdict = verify_structural(_mutate("reports.ingested", topic="quarantine."))
    assert verdict.reasons[0].code == CODE_UNKNOWN_CONTRACT


def test_not_a_mapping_is_rejected() -> None:
    verdict = verify_structural({"topic": uid(), "schema_version": "1.0.0"})
    assert not verdict.passed
    assert verdict.reasons[0].code in {CODE_UNKNOWN_CONTRACT, CODE_NOT_AN_OBJECT}
