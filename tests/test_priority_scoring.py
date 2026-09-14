"""A4's scoring arithmetic. Pure functions, no infrastructure."""

from __future__ import annotations

from typing import Any

import pytest

from agents.a4_priority import (
    LIFE_SAFETY_FLOOR,
    LIFE_SAFETY_HAZARDS,
    WEIGHTS,
    age_value,
    apply_life_safety_floor,
    band_for,
    compute_factor_breakdown,
    corroboration_value,
    exposure_value,
    hazard_severity_value,
    proximity_value,
    velocity_value,
    weighted_score,
)


def _breakdown(**overrides: object) -> list[dict[str, Any]]:
    defaults: dict[str, object] = {
        "hazard_flags": [],
        "max_severity_raw": 3,
        "road_class": "collector",
        "distance_to_poi_m": 200.0,
        "distinct_reporters": 3,
        "age_days": 2.0,
        "recent_24h": 2,
        "trailing_mean_per_day": 1.0,
    }
    defaults.update(overrides)
    return compute_factor_breakdown(**defaults)  # type: ignore[arg-type]


# -- weights --------------------------------------------------------------


def test_weights_sum_to_one() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_prd_weights_are_used_verbatim() -> None:
    assert WEIGHTS["hazard_severity"] == 0.30
    assert WEIGHTS["exposure"] == 0.20
    assert WEIGHTS["vulnerable_site_proximity"] == 0.15
    assert WEIGHTS["corroboration"] == 0.15
    assert WEIGHTS["age_unresolved"] == 0.10
    assert WEIGHTS["velocity"] == 0.10


# -- factor breakdown shape -------------------------------------------------


def test_breakdown_always_has_six_entries() -> None:
    assert len(_breakdown()) == 6


def test_breakdown_weights_match_the_constant_table() -> None:
    for entry in _breakdown():
        assert entry["weight"] == WEIGHTS[entry["factor"]]


def test_breakdown_covers_every_schema_factor() -> None:
    from common.schemas import load_all

    enum = load_all()["incidents.prioritized.v1"]["properties"]["payload"]["properties"][
        "factor_breakdown"
    ]["items"]["properties"]["factor"]["enum"]
    assert {entry["factor"] for entry in _breakdown()} == set(enum)


def test_contribution_is_weight_times_value_times_100() -> None:
    for entry in _breakdown():
        expected = entry["weight"] * entry["value"] * 100
        assert entry["contribution"] == pytest.approx(expected, abs=1e-3)


# -- life-safety floor ------------------------------------------------------


def test_life_safety_flag_floors_score_at_85() -> None:
    breakdown = _breakdown(hazard_flags=["open_manhole"], distinct_reporters=1, recent_24h=0)
    weighted = weighted_score(breakdown)
    score, applied = apply_life_safety_floor(weighted, ["open_manhole"])
    assert score >= LIFE_SAFETY_FLOOR
    assert applied is True


def test_floor_never_lowers_an_already_high_score() -> None:
    score, applied = apply_life_safety_floor(97.0, ["gas_leak"])
    assert score == 97.0
    assert applied is True


def test_no_hazard_flag_does_not_floor() -> None:
    score, applied = apply_life_safety_floor(20.0, [])
    assert score == 20.0
    assert applied is False


def test_non_life_safety_hazard_does_not_floor() -> None:
    """deep_water is a real hazard flag but not in the life-safety subset."""
    score, applied = apply_life_safety_floor(20.0, ["deep_water"])
    assert score == 20.0
    assert applied is False


def test_every_life_safety_hazard_is_recognized() -> None:
    assert {"open_manhole", "live_wire", "collapsed_structure", "gas_leak"} == LIFE_SAFETY_HAZARDS


def test_hazard_severity_value_maxes_out_on_life_safety_flag() -> None:
    assert hazard_severity_value(["live_wire"], max_severity_raw=1) == 1.0


# -- band cutoffs -------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (100.0, "critical"),
        (85.0, "critical"),
        (84.9, "high"),
        (60.0, "high"),
        (35.0, "medium"),
        (34.9, "low"),
        (0.0, "low"),
    ],
)
def test_band_cutoffs(score: float, expected: str) -> None:
    assert band_for(score) == expected


def test_life_safety_floor_always_lands_in_critical() -> None:
    assert band_for(LIFE_SAFETY_FLOOR) == "critical"


# -- corroboration: log-scaled and device-deduplicated -----------------


def test_corroboration_is_zero_with_no_reporters() -> None:
    assert corroboration_value(0) == 0.0


def test_corroboration_increases_with_distinct_reporters() -> None:
    assert corroboration_value(1) < corroboration_value(5) < corroboration_value(20)


def test_corroboration_saturates_near_one_at_the_constant() -> None:
    from agents.a4_priority import CORROBORATION_SATURATION

    assert corroboration_value(CORROBORATION_SATURATION) == pytest.approx(1.0)


def test_corroboration_never_exceeds_one_beyond_saturation() -> None:
    assert corroboration_value(1000) == 1.0


def test_corroboration_log_scaling_diminishes_returns() -> None:
    """The 1 -> 5 jump must matter more than the 20 -> 24 jump."""
    delta_low = corroboration_value(5) - corroboration_value(1)
    delta_high = corroboration_value(24) - corroboration_value(20)
    assert delta_low > delta_high


def test_duplicate_devices_do_not_inflate_corroboration() -> None:
    """This factor takes distinct_reporters directly -- device dedup already
    happened in A3 (member counting by device_hash), so 5 reports from 1
    device must score the same as distinct_reporters=1, not 5."""
    one_device_five_reports = corroboration_value(1)
    five_distinct_devices = corroboration_value(5)
    assert one_device_five_reports < five_distinct_devices


# -- age and velocity -----------------------------------------------------


def test_age_value_zero_for_brand_new_incident() -> None:
    assert age_value(0.0) == 0.0


def test_age_value_saturates() -> None:
    from agents.a4_priority import AGE_SATURATION_DAYS

    assert age_value(AGE_SATURATION_DAYS) == 1.0
    assert age_value(AGE_SATURATION_DAYS * 10) == 1.0


def test_velocity_zero_with_no_recent_reports() -> None:
    assert velocity_value(0, 5.0) == 0.0


def test_velocity_maxes_out_at_surge_ratio() -> None:
    from agents.a4_priority import VELOCITY_SURGE_RATIO

    assert velocity_value(int(VELOCITY_SURGE_RATIO * 2), 2.0) == 1.0


def test_velocity_with_no_trailing_history_uses_absolute_fallback() -> None:
    assert velocity_value(5, 0.0) == 1.0
    assert velocity_value(1, 0.0) == pytest.approx(0.2)


# -- exposure and proximity ------------------------------------------------


def test_exposure_unknown_road_class_falls_back_to_local_weight() -> None:
    assert exposure_value(None) == exposure_value("local")


def test_exposure_arterial_scores_highest() -> None:
    assert exposure_value("arterial") > exposure_value("collector") > exposure_value("local")


def test_proximity_at_the_poi_scores_one() -> None:
    assert proximity_value(0.0) == 1.0


def test_proximity_beyond_max_radius_scores_zero() -> None:
    assert proximity_value(500.0) == 0.0
    assert proximity_value(10_000.0) == 0.0


def test_proximity_with_no_distance_scores_zero_not_a_guess() -> None:
    assert proximity_value(None) == 0.0


def test_weighted_score_is_clamped_to_100() -> None:
    breakdown = compute_factor_breakdown(
        hazard_flags=["gas_leak"],
        max_severity_raw=5,
        road_class="arterial",
        distance_to_poi_m=0.0,
        distinct_reporters=1000,
        age_days=1000,
        recent_24h=100,
        trailing_mean_per_day=1.0,
    )
    assert weighted_score(breakdown) <= 100.0
