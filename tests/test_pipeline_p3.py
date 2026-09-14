"""P3 acceptance (PRD milestone M2): A4 Prioritization and A5 Routing end to end.

Runs the full core path (A0-A3) plus A4 and A5 against Postgres/PostGIS, using
the M1 seed scenario as a source of real incidents to score and route. Skipped
when the stack is down, exactly like test_pipeline_m1.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.a4_priority import PriorityAgent
from agents.a5_routing import RoutingAgent
from bus.memory import InMemoryBus
from common.db import connect
from scripts import seed_m1
from scripts.load_reference_data import load as load_reference
from scripts.load_reference_data import load_poi
from tests.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


@pytest.fixture
def routed(clean_db: None) -> dict[str, Any]:
    """Drive the full pipeline: A1-A3 consolidate, then A4 prioritizes and A5 routes."""
    load_reference()
    load_poi()

    bus = InMemoryBus()
    from agents.a1_perception import PerceptionAgent
    from agents.a2_dedup import DedupAgent
    from agents.a3_synthesis import SynthesisAgent

    priority = PriorityAgent(bus, consumer="p3-a4")
    routing = RoutingAgent(bus, consumer="p3-a5")

    for probe in (
        PerceptionAgent(bus),
        DedupAgent(bus),
        SynthesisAgent(bus),
        priority,
        routing,
    ):
        bus.create_group(probe.input_topic, probe.group)

    seed_m1.seed(bus, seed_m1.load_scenario())
    handled = seed_m1.drain(bus)

    prioritized = 0
    routed_count = 0
    for _ in range(40):
        results = priority.run_once(count=10)
        prioritized += sum(len(r.emitted) for r in results)
        route_results = routing.run_once(count=10)
        routed_count += sum(len(r.emitted) for r in route_results)
        if not results and not route_results:
            break

    return {"bus": bus, "handled": handled, "prioritized": prioritized, "routed": routed_count}


def test_every_incident_was_prioritized(routed: dict[str, Any]) -> None:
    unscored = _rows("SELECT 1 FROM incidents WHERE priority_score IS NULL")
    assert unscored == []


def test_every_score_has_a_six_item_breakdown(routed: dict[str, Any]) -> None:
    rows = _rows("SELECT factor_breakdown FROM incidents")
    assert rows
    for row in rows:
        assert len(row["factor_breakdown"]) == 6


def test_every_priority_band_is_a_valid_enum_value(routed: dict[str, Any]) -> None:
    rows = _rows("SELECT DISTINCT priority_band FROM incidents")
    assert {row["priority_band"] for row in rows} <= {"critical", "high", "medium", "low"}


def test_every_incident_reachable_by_category_was_routed(routed: dict[str, Any]) -> None:
    """Every M1 seed category maps to a department (none is 'other'), so every
    incident must end up routed, not unrouted."""
    unrouted = _rows("SELECT 1 FROM incidents WHERE status <> 'routed'")
    assert unrouted == []


def test_routed_incidents_have_a_real_department(routed: dict[str, Any]) -> None:
    rows = _rows(
        """
        SELECT i.department_id FROM incidents i
        LEFT JOIN departments d ON d.department_id = i.department_id
        WHERE i.department_id IS NOT NULL AND d.department_id IS NULL
        """
    )
    assert rows == []


def test_routed_incidents_have_an_sla_due_date(routed: dict[str, Any]) -> None:
    rows = _rows("SELECT 1 FROM incidents WHERE status = 'routed' AND sla_due_at IS NULL")
    assert rows == []


def test_waterlogging_incidents_cc_roads(routed: dict[str, Any]) -> None:
    rows = _rows(
        "SELECT cc_departments FROM incidents WHERE category IN ('waterlogging', 'drain_overflow')"
    )
    for row in rows:
        assert "roads_and_infrastructure" in row["cc_departments"]


def test_no_agent_errored(routed: dict[str, Any]) -> None:
    errors = _rows("SELECT agent, error FROM agent_runs WHERE outcome = 'error'")
    assert errors == []


def test_life_safety_incidents_are_floored_at_85(routed: dict[str, Any]) -> None:
    """Any incident whose members carry a life-safety hazard flag must score
    at least 85, regardless of report count."""
    rows = _rows(
        """
        SELECT i.incident_id, i.priority_score
        FROM incidents i
        WHERE EXISTS (
            SELECT 1 FROM reports r
            WHERE r.incident_id = i.incident_id
              AND r.hazard_flags && ARRAY[
                  'open_manhole', 'live_wire', 'collapsed_structure', 'gas_leak'
              ]
        )
        """
    )
    for row in rows:
        assert row["priority_score"] >= 85.0
