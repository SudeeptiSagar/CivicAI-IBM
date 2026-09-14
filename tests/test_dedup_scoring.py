"""A2's scoring arithmetic and A3's grey-zone adjudicator.

Pure functions, no infrastructure. The database side of A2 and A3 is covered by
the integration tests in test_pipeline_m1.py.
"""

from __future__ import annotations

import pytest

from agents.a2_dedup import (
    AUTO_LINK_THRESHOLD,
    CANDIDATE_THRESHOLD,
    CATEGORY_RADIUS_M,
    CATEGORY_WINDOW_DAYS,
    WEIGHTS,
    Candidate,
    DedupAgent,
)
from agents.a3_synthesis import ADJUDICATION_FLOORS, adjudicate


def _candidate(
    spatial: float = 0.9,
    temporal: float = 0.9,
    semantic: float = 0.9,
    visual: float | None = None,
) -> Candidate:
    return Candidate(
        incident_id="00000000-0000-7000-8000-000000000001",
        spatial=spatial,
        temporal=temporal,
        semantic=semantic,
        visual=visual,
    )


# -- weights and thresholds ----------------------------------------------


def test_weights_sum_to_one() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_thresholds_match_the_prd() -> None:
    assert AUTO_LINK_THRESHOLD == 0.82
    assert CANDIDATE_THRESHOLD == 0.60


def test_prd_named_radii_are_used_verbatim() -> None:
    """PRD section 7/A2 names three radii explicitly."""
    assert CATEGORY_RADIUS_M["pothole"] == 75.0
    assert CATEGORY_RADIUS_M["garbage"] == 150.0
    assert CATEGORY_RADIUS_M["waterlogging"] == 300.0


def test_prd_named_windows_are_used_verbatim() -> None:
    assert CATEGORY_WINDOW_DAYS["pothole"] == 21.0
    assert CATEGORY_WINDOW_DAYS["waterlogging"] == 3.0
    assert CATEGORY_WINDOW_DAYS["garbage"] == 7.0


def test_every_category_has_a_radius_and_window() -> None:
    """A category with no bound would silently fall back to the default and
    dedup at the wrong distance."""
    from common.schemas import load_all

    categories = set(load_all()["envelope.v1"]["$defs"]["category"]["enum"])
    assert categories <= set(CATEGORY_RADIUS_M)
    assert categories <= set(CATEGORY_WINDOW_DAYS)


# -- scoring -------------------------------------------------------------


def test_perfect_match_scores_one() -> None:
    assert _candidate(1.0, 1.0, 1.0, 1.0).score == pytest.approx(1.0)


def test_zero_match_scores_zero() -> None:
    assert _candidate(0.0, 0.0, 0.0, 0.0).score == pytest.approx(0.0)


def test_missing_visual_renormalises_rather_than_scoring_zero() -> None:
    """Scoring an unavailable component as zero would drag every match below
    the threshold and defeat dedup entirely. The available weights are
    renormalised instead."""
    with_visual = _candidate(0.9, 0.9, 0.9, 0.9)
    without_visual = _candidate(0.9, 0.9, 0.9, None)

    assert without_visual.score == pytest.approx(with_visual.score)


def test_renormalisation_is_not_a_free_boost() -> None:
    """A three-component score must still reflect its three components."""
    assert _candidate(0.9, 0.9, 0.2, None).score < _candidate(0.9, 0.9, 0.9, None).score


def test_all_components_missing_scores_zero() -> None:
    candidate = Candidate("i", 0.0, 0.0, 0.0, None)
    assert candidate.score == 0.0


def test_component_scores_report_the_missing_one_as_null() -> None:
    """The arithmetic has to stay auditable: a reader must be able to see which
    components the score was actually computed from."""
    assert _candidate(visual=None).component_scores()["visual"] is None


# -- the decision bands --------------------------------------------------


@pytest.mark.parametrize(
    ("score_shape", "expected"),
    [
        ((0.95, 0.95, 0.95), "auto_link"),
        ((0.90, 0.90, 0.70), "candidate_link"),
        ((0.30, 0.30, 0.30), "new_incident_seed"),
    ],
)
def test_decision_bands(score_shape: tuple[float, float, float], expected: str) -> None:
    decision, _ = DedupAgent._decide(_candidate(*score_shape))
    assert decision == expected


def test_no_candidates_seeds_a_new_incident() -> None:
    decision, incident_id = DedupAgent._decide(None)
    assert decision == "new_incident_seed"
    assert incident_id is None


def test_auto_link_carries_the_incident_id() -> None:
    _, incident_id = DedupAgent._decide(_candidate(1.0, 1.0, 1.0))
    assert incident_id == "00000000-0000-7000-8000-000000000001"


def test_seed_decision_does_not_carry_an_incident_id() -> None:
    """A new seed must not point at the incident it failed to match, or A3
    would attach it there anyway."""
    _, incident_id = DedupAgent._decide(_candidate(0.1, 0.1, 0.1))
    assert incident_id is None


# -- confidence ----------------------------------------------------------


def test_grey_zone_midpoint_is_the_least_confident_state() -> None:
    """Which is exactly why the PRD sends that band to A3 to arbitrate."""
    midpoint = (AUTO_LINK_THRESHOLD + CANDIDATE_THRESHOLD) / 2
    candidate = _candidate(midpoint, midpoint, midpoint)

    confidence = DedupAgent._confidence(candidate, "candidate_link")

    assert confidence < 0.15


def test_confident_auto_link_reports_high_confidence() -> None:
    assert DedupAgent._confidence(_candidate(1.0, 1.0, 1.0), "auto_link") > 0.9


def test_nothing_nearby_is_a_confident_answer() -> None:
    assert DedupAgent._confidence(None, "new_incident_seed") > 0.8


# -- the adjudicator -----------------------------------------------------


def test_adjudicator_merges_on_strong_geometry() -> None:
    verdict = adjudicate({"spatial": 0.9, "temporal": 0.9, "semantic": 0.5, "visual": None})
    assert verdict.merge
    assert verdict.method == "deterministic"


def test_adjudicator_declines_on_weak_spatial() -> None:
    """Spatial and temporal carry the decision because they are measured, not
    inferred; a weak one must block the merge."""
    verdict = adjudicate({"spatial": 0.5, "temporal": 0.99, "semantic": 0.9, "visual": None})
    assert not verdict.merge
    assert "spatial" in verdict.reason


def test_adjudicator_declines_on_weak_temporal() -> None:
    verdict = adjudicate({"spatial": 0.99, "temporal": 0.4, "semantic": 0.9, "visual": None})
    assert not verdict.merge
    assert "temporal" in verdict.reason


def test_adjudicator_declines_when_the_text_is_about_something_else() -> None:
    """Geometry alone must not merge two different problems at one address."""
    verdict = adjudicate({"spatial": 0.99, "temporal": 0.99, "semantic": 0.05, "visual": None})
    assert not verdict.merge
    assert "semantic" in verdict.reason


def test_adjudicator_declines_on_missing_components() -> None:
    verdict = adjudicate({})
    assert not verdict.merge


def test_adjudicator_reason_is_always_populated() -> None:
    """It lands in the incident's rationale, which is what a reviewer reads."""
    for scores in (
        {"spatial": 0.9, "temporal": 0.9, "semantic": 0.9},
        {"spatial": 0.1, "temporal": 0.1, "semantic": 0.1},
    ):
        assert adjudicate(scores).reason


def test_adjudication_floors_are_stricter_than_candidate_generation() -> None:
    """The floors must mean "comfortably inside the category bounds", not
    merely "inside them" — candidate generation already guarantees the latter."""
    assert ADJUDICATION_FLOORS["spatial"] > 0.5
    assert ADJUDICATION_FLOORS["temporal"] > 0.5


def test_adjudicated_merge_never_reaches_the_auto_link_band() -> None:
    """Anything scoring >= 0.82 is A2's call, not the adjudicator's; the
    adjudicator exists only for the band A2 could not decide."""
    from agents.a2_dedup import AUTO_LINK_THRESHOLD as auto

    grey = _candidate(0.9, 0.9, 0.45)
    assert CANDIDATE_THRESHOLD <= grey.score < auto
    assert adjudicate(grey.component_scores()).merge
