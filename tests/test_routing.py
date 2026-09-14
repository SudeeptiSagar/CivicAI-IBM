"""A5's routing policy. Pure functions, no infrastructure."""

from __future__ import annotations

import datetime as dt

import pytest

from agents.a5_routing import (
    CATEGORY_DEPARTMENT,
    SLA_BAND_MULTIPLIER,
    cc_departments_for,
    department_for_category,
    sla_due_at_for,
    sla_hours_for,
)

DEPARTMENTS = {
    "roads_and_infrastructure",
    "drainage_and_water",
    "solid_waste_management",
    "electrical_streetlights",
    "health",
    "parks",
}


# -- the category -> department map ----------------------------------------


def test_every_mapped_department_is_a_real_department() -> None:
    assert set(CATEGORY_DEPARTMENT.values()) <= DEPARTMENTS


def test_every_category_in_the_envelope_taxonomy_is_covered_except_other() -> None:
    from common.schemas import load_all

    categories = set(load_all()["envelope.v1"]["$defs"]["category"]["enum"])
    assert categories - {"other"} <= set(CATEGORY_DEPARTMENT)
    assert "other" not in CATEGORY_DEPARTMENT


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("pothole", "roads_and_infrastructure"),
        ("road_damage", "roads_and_infrastructure"),
        ("encroachment", "roads_and_infrastructure"),
        ("traffic_obstruction", "roads_and_infrastructure"),
        ("waterlogging", "drainage_and_water"),
        ("drain_overflow", "drainage_and_water"),
        ("water_supply", "drainage_and_water"),
        ("sewage", "health"),
        ("garbage", "solid_waste_management"),
        ("public_toilet", "solid_waste_management"),
        ("streetlight", "electrical_streetlights"),
        ("stray_animals", "health"),
        ("tree_hazard", "parks"),
    ],
)
def test_category_department_mapping(category: str, expected: str) -> None:
    assert department_for_category(category) == expected


def test_other_is_unmapped() -> None:
    assert department_for_category("other") is None


def test_none_category_is_unmapped() -> None:
    assert department_for_category(None) is None


def test_unknown_category_is_unmapped() -> None:
    assert department_for_category("not_a_real_category") is None


# -- ambiguous ownership ----------------------------------------------------


def test_waterlogging_cc_s_roads() -> None:
    cc = cc_departments_for("waterlogging", "drainage_and_water")
    assert cc == ["roads_and_infrastructure"]


def test_drain_overflow_cc_s_roads() -> None:
    cc = cc_departments_for("drain_overflow", "drainage_and_water")
    assert cc == ["roads_and_infrastructure"]


def test_pothole_has_no_cc() -> None:
    assert cc_departments_for("pothole", "roads_and_infrastructure") == []


def test_cc_never_repeats_the_primary_department() -> None:
    """Defensive: if a future rule's cc set ever included its own primary."""
    cc = cc_departments_for("waterlogging", "roads_and_infrastructure")
    assert "roads_and_infrastructure" not in cc


# -- SLA clock ---------------------------------------------------------


def test_sla_multiplier_table_orders_bands_correctly() -> None:
    assert (
        SLA_BAND_MULTIPLIER["critical"]
        < SLA_BAND_MULTIPLIER["high"]
        < SLA_BAND_MULTIPLIER["medium"]
        < SLA_BAND_MULTIPLIER["low"]
    )


@pytest.mark.parametrize(
    ("band", "expected_hours"),
    [("critical", 12.0), ("high", 24.0), ("medium", 48.0), ("low", 72.0)],
)
def test_sla_hours_for_band(band: str, expected_hours: float) -> None:
    assert sla_hours_for(48.0, band) == expected_hours


def test_sla_hours_falls_back_to_multiplier_one_for_unknown_band() -> None:
    assert sla_hours_for(48.0, "not_a_band") == 48.0


def test_sla_due_at_adds_hours_to_base_time() -> None:
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    due = sla_due_at_for(base, 24.0)
    assert due == base + dt.timedelta(hours=24)


def test_critical_incident_gets_a_shorter_window_than_default() -> None:
    default_hours = 72.0
    critical_hours = sla_hours_for(default_hours, "critical")
    low_hours = sla_hours_for(default_hours, "low")
    assert critical_hours < default_hours < low_hours
