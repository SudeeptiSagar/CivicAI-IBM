"""Sentinel L2 - invariant / business rules (PRD section 8.1, P4).

Runs entirely against `tests/factories.py` fixtures and `allow_db=False`, so
every self-contained rule is covered without a database. The DB-gated rules
(location inside city, report_id uniqueness, incident membership, ward
registry) are exercised only by the integration suite
(`tests/test_pipeline_p4.py`), which skips without Postgres — consistent with
every other DB-dependent test in this repo.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from agents.av_sentinel.layers.invariants import has_rules_for, verify_invariants
from agents.av_sentinel.rules import a1, a5
from common.topics import TOPICS
from tests.factories import envelope_for

# -- coverage --------------------------------------------------------------


def test_has_rules_for_the_seven_documented_topics() -> None:
    covered = {
        "reports.ingested",
        "reports.understood",
        "reports.linked",
        "incidents.updated",
        "incidents.prioritized",
        "incidents.routed",
        "incidents.unrouted",
    }
    assert {t for t in TOPICS if has_rules_for(t)} == covered


def test_no_rules_for_topics_without_an_agent_row() -> None:
    """A6/A7 and everything else outside PRD 8.1's table get no L2 rules."""
    for topic in ("evidence.attached", "resolution.claimed", "resolution.verified", "deadletter"):
        assert not has_rules_for(topic)


# -- the happy path: every factory envelope passes L2 -----------------------


@pytest.mark.parametrize(
    "topic",
    [
        "reports.ingested",
        "reports.understood",
        "reports.linked",
        "incidents.updated",
        "incidents.prioritized",
        "incidents.routed",
        "incidents.unrouted",
    ],
)
def test_valid_factory_envelope_passes_l2(topic: str) -> None:
    verdict = verify_invariants(topic, envelope_for(topic), allow_db=False)
    assert verdict.passed, verdict.reasons
    assert verdict.verdict == "pass"


# -- A0: media present for has_photo -----------------------------------


def test_a0_has_photo_without_media_fails_hard() -> None:
    envelope = envelope_for("reports.ingested")
    envelope["payload"]["media_keys"] = []
    verdict = verify_invariants("reports.ingested", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert verdict.reasons[0].code == "a0_has_photo_without_media"


def test_a0_no_photo_no_media_is_fine() -> None:
    envelope = envelope_for("reports.ingested")
    envelope["payload"]["has_photo"] = False
    envelope["payload"]["media_keys"] = []
    assert verify_invariants("reports.ingested", envelope, allow_db=False).passed


# -- A1: category, summary, embedding, modality_conflict --------------------


def test_a1_category_outside_taxonomy_fails_hard() -> None:
    envelope = envelope_for("reports.understood")
    envelope["payload"]["category"] = "not_a_real_category"
    verdict = verify_invariants("reports.understood", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert verdict.reasons[0].code == "a1_category_outside_taxonomy"


def test_a1_summary_over_word_cap_is_fail_soft() -> None:
    envelope = envelope_for("reports.understood")
    envelope["payload"]["summary"] = " ".join(f"word{i}" for i in range(25))
    verdict = verify_invariants("reports.understood", envelope, allow_db=False)
    assert verdict.verdict == "fail_soft"
    assert verdict.reasons[0].code == "a1_summary_too_long"


def test_a1_all_zero_embedding_fails_hard() -> None:
    envelope = envelope_for("reports.understood")
    envelope["payload"]["embedding"] = [0.0] * 768
    verdict = verify_invariants("reports.understood", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert verdict.reasons[0].code == "a1_embedding_all_zero"


def test_a1_modality_conflict_mismatch_is_a_warn_not_a_failure() -> None:
    envelope = envelope_for("reports.understood")
    envelope["payload"]["category"] = "garbage"
    envelope["payload"]["vision_labels"] = ["streetlight", "lamp"]
    envelope["payload"]["modality_conflict"] = False
    verdict = verify_invariants("reports.understood", envelope, allow_db=False)
    assert verdict.verdict == "warn"
    assert verdict.passed  # warn still proceeds
    assert verdict.reasons[0].code == "a1_modality_conflict_maybe_missed"


def test_a1_modality_conflict_already_true_is_not_re_flagged() -> None:
    envelope = envelope_for("reports.understood")
    envelope["payload"]["category"] = "garbage"
    envelope["payload"]["vision_labels"] = ["streetlight"]
    envelope["payload"]["modality_conflict"] = True
    assert verify_invariants("reports.understood", envelope, allow_db=False).passed


# -- A2: decision must match match_score -------------------------------


@pytest.mark.parametrize(
    ("score", "wrong_decision"),
    [(0.90, "candidate_link"), (0.70, "auto_link"), (0.30, "candidate_link")],
)
def test_a2_decision_score_mismatch_fails_hard(score: float, wrong_decision: str) -> None:
    envelope = envelope_for("reports.linked")
    envelope["payload"]["match_score"] = score
    envelope["payload"]["decision"] = wrong_decision
    verdict = verify_invariants("reports.linked", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert verdict.reasons[0].code == "a2_decision_score_mismatch"


@pytest.mark.parametrize(
    ("score", "decision"),
    [(0.90, "auto_link"), (0.70, "candidate_link"), (0.30, "new_incident_seed")],
)
def test_a2_decision_score_agreement_passes(score: float, decision: str) -> None:
    envelope = envelope_for("reports.linked")
    envelope["payload"]["match_score"] = score
    envelope["payload"]["decision"] = decision
    assert verify_invariants("reports.linked", envelope, allow_db=False).passed


# -- A3: report_count vs member_report_ids -----------------------------


def test_a3_report_count_mismatch_fails_hard() -> None:
    envelope = envelope_for("incidents.updated")
    envelope["payload"]["report_count"] = 99
    verdict = verify_invariants("incidents.updated", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert verdict.reasons[0].code == "a3_report_count_mismatch"


# -- A4: weight sum, life-safety floor, score recomputation ------------


def test_a4_weights_not_summing_to_one_fails_hard() -> None:
    envelope = envelope_for("incidents.prioritized")
    breakdown = envelope["payload"]["factor_breakdown"]
    breakdown[0]["weight"] = 0.99  # was 0.30; sum now way off 1.0
    verdict = verify_invariants("incidents.prioritized", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert any(r.code == "a4_weights_do_not_sum_to_one" for r in verdict.reasons)


def test_a4_life_safety_floor_not_honoured_fails_hard() -> None:
    envelope = envelope_for("incidents.prioritized")
    envelope["payload"]["life_safety_floor_applied"] = True
    envelope["payload"]["priority_score"] = 40.0
    verdict = verify_invariants("incidents.prioritized", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert any(r.code == "a4_life_safety_floor_not_honoured" for r in verdict.reasons)


def test_a4_floor_applied_skips_the_arithmetic_recomputation() -> None:
    """The floor overrides the weighted sum by definition; a floored score
    that does not match the raw contribution sum must not be flagged."""
    envelope = envelope_for("incidents.prioritized")
    envelope["payload"]["life_safety_floor_applied"] = True
    envelope["payload"]["priority_score"] = 85.0
    assert verify_invariants("incidents.prioritized", envelope, allow_db=False).passed


def test_a4_score_not_matching_breakdown_fails_hard() -> None:
    envelope = envelope_for("incidents.prioritized")
    envelope["payload"]["priority_score"] = 10.0  # factory breakdown sums to ~64.7
    verdict = verify_invariants("incidents.prioritized", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert any(r.code == "a4_score_does_not_match_breakdown" for r in verdict.reasons)


# -- A5: department registry, SLA set -----------------------------------


def test_a5_department_off_registry_fails_hard() -> None:
    envelope = envelope_for("incidents.routed")
    envelope["payload"]["primary_department"] = "made_up_department"
    verdict = verify_invariants("incidents.routed", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert verdict.reasons[0].code == "a5_department_off_registry"


def test_a5_sla_not_set_fails_hard() -> None:
    envelope = envelope_for("incidents.routed")
    envelope["payload"]["sla_hours"] = 0.0
    verdict = verify_invariants("incidents.routed", envelope, allow_db=False)
    assert verdict.verdict == "fail_hard"
    assert any(r.code == "a5_sla_not_set" for r in verdict.reasons)


def test_a5_department_registry_mirrors_migration_0001() -> None:
    """Drift guard: db/migrations/0001_init.sql seeds exactly these six
    department_id rows. If one repo drifts, this test catches it."""
    assert {
        "roads_and_infrastructure",
        "drainage_and_water",
        "solid_waste_management",
        "electrical_streetlights",
        "health",
        "parks",
    } == a5.DEPARTMENT_REGISTRY


def test_a1_category_taxonomy_mirrors_the_envelope_schema() -> None:
    """Drift guard: mirrors schemas/envelope.v1.json#/$defs/category."""
    import json
    import pathlib

    schema_path = pathlib.Path(__file__).resolve().parent.parent / "schemas" / "envelope.v1.json"
    schema: dict[str, Any] = json.loads(schema_path.read_text(encoding="utf-8"))
    assert set(schema["$defs"]["category"]["enum"]) == a1.CATEGORY_TAXONOMY


# -- unaffected topics do not get rubber-stamped --------------------------


def test_unmapped_topic_returns_pass_with_no_reasons() -> None:
    verdict = verify_invariants(
        "evidence.attached", envelope_for("evidence.attached"), allow_db=False
    )
    assert verdict.verdict == "pass"
    assert verdict.reasons == []


# -- non-mapping payload is a no-op, not a crash ---------------------------


def test_missing_payload_produces_no_findings() -> None:
    envelope = envelope_for("reports.ingested")
    del envelope["payload"]
    verdict = verify_invariants("reports.ingested", envelope, allow_db=False)
    assert verdict.verdict == "pass"


def test_original_envelope_is_never_mutated() -> None:
    """PRD section 8.2: Sentinel can flag and block, but never fix."""
    envelope = envelope_for("incidents.prioritized")
    snapshot = copy.deepcopy(envelope)
    verify_invariants("incidents.prioritized", envelope, allow_db=False)
    assert envelope == snapshot
