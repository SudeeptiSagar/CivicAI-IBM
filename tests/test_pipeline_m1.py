"""M1 acceptance (PRD section 13).

    "20 seeded reports collapse into the expected incidents"

Runs the real core path — A0, A1, A2, A3 — against Postgres with PostGIS and
pgvector. The expected clustering is declared in data/seed/m1_reports.json and
asserted here, so a change that quietly over- or under-merges fails the build.

Skipped when the stack is down.
"""

from __future__ import annotations

from typing import Any

import pytest

from bus.memory import InMemoryBus
from common.db import connect
from scripts import seed_m1
from scripts.load_reference_data import load as load_reference
from tests.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]


@pytest.fixture
def scenario() -> dict[str, Any]:
    return seed_m1.load_scenario()


@pytest.fixture
def seeded(clean_db: None, scenario: dict[str, Any]) -> dict[str, Any]:
    """Seed the 20 reports and drive A1/A2/A3 to completion.

    Uses the in-memory bus: the scenario is about what the agents decide, and a
    real broker would only add a scheduler to the test. The Redis path is
    covered in test_pipeline_m0.py.
    """
    load_reference()

    bus = InMemoryBus()
    from agents.a1_perception import PerceptionAgent
    from agents.a2_dedup import DedupAgent
    from agents.a3_synthesis import SynthesisAgent

    # Groups must exist before the seeding publishes, or a consumer starting
    # at the tail never sees the backlog.
    for probe in (
        PerceptionAgent(bus, provider=_provider()),
        DedupAgent(bus),
        SynthesisAgent(bus),
    ):
        bus.create_group(probe.input_topic, probe.group)

    report_ids = seed_m1.seed(bus, scenario)
    handled = seed_m1.drain(bus)

    return {"bus": bus, "report_ids": report_ids, "handled": handled, "scenario": scenario}


def _provider() -> Any:
    from common.llm.heuristic import HeuristicProvider

    return HeuristicProvider()


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _incidents() -> list[dict[str, Any]]:
    return _rows(
        "SELECT incident_id, category, report_count, distinct_reporters, ward_id, title "
        "FROM incidents ORDER BY report_count DESC, category"
    )


# -- the acceptance criterion --------------------------------------------


def test_every_seeded_report_is_ingested(seeded: dict[str, Any]) -> None:
    assert len(seeded["report_ids"]) == 20
    assert _rows("SELECT 1 FROM reports")[0:20] != []
    assert len(_rows("SELECT 1 FROM reports")) == 20


def test_every_agent_handled_every_message(seeded: dict[str, Any]) -> None:
    assert seeded["handled"] == {"A1": 20, "A2": 20, "A3": 20}


def test_no_agent_errored(seeded: dict[str, Any]) -> None:
    errors = _rows("SELECT agent, error FROM agent_runs WHERE outcome = 'error'")
    assert errors == []


def test_reports_collapse_into_the_expected_incident_count(seeded: dict[str, Any]) -> None:
    """M1's headline criterion."""
    expected = len(seeded["scenario"]["expected_incidents"])
    assert len(_incidents()) == expected


def test_each_expected_cluster_is_present(seeded: dict[str, Any]) -> None:
    """Not just the right number of incidents — the right shapes.

    Compared as a multiset of (category, report_count, distinct_reporters), so
    an over-merge that happened to cancel out an under-merge still fails.
    """
    expected = sorted(
        (e["category"], e["reports"], e["distinct_reporters"])
        for e in seeded["scenario"]["expected_incidents"]
    )
    actual = sorted(
        (row["category"], row["report_count"], row["distinct_reporters"]) for row in _incidents()
    )
    assert actual == expected


def test_every_report_belongs_to_exactly_one_incident(seeded: dict[str, Any]) -> None:
    """PRD section 8.1, A2 invariant: no report in two incidents."""
    orphans = _rows("SELECT report_id FROM reports WHERE incident_id IS NULL")
    assert orphans == []

    totals = _rows("SELECT sum(report_count) AS n FROM incidents")
    assert int(totals[0]["n"]) == 20


def test_report_counts_match_actual_membership(seeded: dict[str, Any]) -> None:
    """PRD section 8.1, A3 invariant: report_count equals the member count."""
    drift = _rows(
        """
        SELECT i.incident_id, i.report_count, count(r.report_id) AS actual
        FROM incidents i
        LEFT JOIN reports r ON r.incident_id = i.incident_id
        GROUP BY i.incident_id, i.report_count
        HAVING i.report_count <> count(r.report_id)
        """
    )
    assert drift == []


def test_members_of_an_incident_share_a_category(seeded: dict[str, Any]) -> None:
    """PRD section 8.1, A2 invariant. A2 never merges across categories."""
    mixed = _rows(
        """
        SELECT incident_id FROM reports
        WHERE incident_id IS NOT NULL
        GROUP BY incident_id
        HAVING count(DISTINCT category) > 1
        """
    )
    assert mixed == []


# -- the specific traps in the fixture -----------------------------------


def test_the_four_pothole_reports_became_one_incident(seeded: dict[str, Any]) -> None:
    """Four people describing one pothole in four different sentences. This is
    the PRD's opening example: 17 tickets, one dangerous pothole."""
    clusters = [
        row for row in _incidents() if row["category"] == "pothole" and row["report_count"] == 4
    ]
    assert len(clusters) == 1
    assert clusters[0]["distinct_reporters"] == 4


def test_the_distant_pothole_stayed_separate(seeded: dict[str, Any]) -> None:
    """Same category, 4 km away — far outside the 75 m pothole radius. Guards
    against merging on category alone."""
    singletons = [
        row for row in _incidents() if row["category"] == "pothole" and row["report_count"] == 1
    ]
    assert len(singletons) == 1


def test_drainage_and_waterlogging_stayed_separate(seeded: dict[str, Any]) -> None:
    """They are ~70 m apart and within each other's window, but different
    categories. Associating them is A3 pattern mode's job as a SuperIncident
    (P5), not A2's (PRD section 7/A2)."""
    categories = {row["category"] for row in _incidents()}
    assert {"waterlogging", "drain_overflow"} <= categories

    assert (
        _rows(
            """
        SELECT 1 FROM reports a JOIN reports b ON a.incident_id = b.incident_id
        WHERE a.category = 'waterlogging' AND b.category = 'drain_overflow'
        """
        )
        == []
    )


def test_repeat_reporter_is_counted_once(seeded: dict[str, Any]) -> None:
    """The garbage cluster has 3 reports from 2 devices. Corroboration is
    device-deduplicated so one person cannot brigade the queue
    (PRD section 7/A4)."""
    garbage = [row for row in _incidents() if row["category"] == "garbage"]
    assert len(garbage) == 1
    assert garbage[0]["report_count"] == 3
    assert garbage[0]["distinct_reporters"] == 2


def test_no_super_incidents_were_created(seeded: dict[str, Any]) -> None:
    """Pattern mode is P5. A3 must not be inventing them early."""
    assert _rows("SELECT 1 FROM super_incidents") == []


# -- quality of the consolidation ----------------------------------------


def test_every_incident_has_a_ward(seeded: dict[str, Any]) -> None:
    """Every seeded coordinate sits inside a fixture ward, so a null here means
    the PostGIS join failed rather than that the point was genuinely outside."""
    assert _rows("SELECT 1 FROM incidents WHERE ward_id IS NULL") == []


def test_centroid_lies_among_its_members(seeded: dict[str, Any]) -> None:
    """A weighted centroid that landed outside its members' bounding box would
    mean the weighting is wrong."""
    outliers = _rows(
        """
        SELECT i.incident_id
        FROM incidents i
        JOIN reports r ON r.incident_id = i.incident_id
        GROUP BY i.incident_id, i.centroid
        HAVING ST_Y(i.centroid::geometry) NOT BETWEEN
                   min(ST_Y(r.geom::geometry)) - 1e-9 AND max(ST_Y(r.geom::geometry)) + 1e-9
            OR ST_X(i.centroid::geometry) NOT BETWEEN
                   min(ST_X(r.geom::geometry)) - 1e-9 AND max(ST_X(r.geom::geometry)) + 1e-9
        """
    )
    assert outliers == []


def test_incident_window_spans_its_members(seeded: dict[str, Any]) -> None:
    bad = _rows(
        """
        SELECT i.incident_id
        FROM incidents i
        JOIN reports r ON r.incident_id = i.incident_id
        GROUP BY i.incident_id, i.first_reported_at, i.last_reported_at
        HAVING i.first_reported_at <> min(r.created_at)
            OR i.last_reported_at <> max(r.created_at)
        """
    )
    assert bad == []


def test_every_report_was_classified(seeded: dict[str, Any]) -> None:
    assert _rows("SELECT 1 FROM reports WHERE category IS NULL") == []


def test_every_report_carries_an_embedding(seeded: dict[str, Any]) -> None:
    """A2's semantic component is meaningless without one."""
    assert _rows("SELECT 1 FROM reports WHERE embedding IS NULL") == []


def test_compression_ratio_is_reported_honestly(seeded: dict[str, Any]) -> None:
    """PRD section 14 targets >= 3x on seeded data. This fixture is ~1.8x by
    design: it is stuffed with singletons specifically to test that A2 does NOT
    over-merge, so the ratio is a property of the fixture rather than a
    measurement of dedup quality. Asserted so the number cannot drift
    unnoticed and be mistaken for the PRD metric."""
    reports = len(_rows("SELECT 1 FROM reports"))
    incidents = len(_incidents())
    assert reports / incidents == pytest.approx(20 / 11, abs=0.01)


# -- concurrency ---------------------------------------------------------


def test_concurrent_reports_of_one_problem_do_not_each_seed(clean_db: None) -> None:
    """Regression, found by running the live stack rather than by a test.

    Agents run concurrently and PRD section 9.4 orders messages only per
    correlation_id. When four citizens report one pothole within a second, A2
    can score all four before A3 has created an incident for any of them: every
    one sees an empty incident table and seeds its own. Four reports, four
    incidents — the exact opposite of what the PRD opens by promising.

    This reproduces that ordering deliberately: every report is taken all the
    way through A2 *before* A3 runs at all. A3's re-check against authoritative
    state is what collapses them.
    """
    from agents.a0_intake import IntakeAgent, Submission
    from agents.a1_perception import PerceptionAgent
    from agents.a2_dedup import DedupAgent
    from agents.a3_synthesis import SynthesisAgent

    load_reference()
    bus = InMemoryBus()

    perception = PerceptionAgent(bus, consumer="race-a1", provider=_provider())
    dedup = DedupAgent(bus, consumer="race-a2")
    synthesis = SynthesisAgent(bus, consumer="race-a3")
    for agent in (perception, dedup, synthesis):
        bus.create_group(agent.input_topic, agent.group)

    intake = IntakeAgent(bus)
    texts = [
        "Huge pothole outside the school gate, very deep and dangerous",
        "Deep pothole near the school gate, autos are swerving around it",
        "Dangerous pothole at the school gate, a child nearly fell in",
        "Pothole outside the school gate is still not fixed after weeks",
    ]
    for index, text in enumerate(texts):
        accepted = intake.intake(
            Submission(
                device_hash=f"race-device-{index}",
                lat=12.9345 + index * 0.00002,
                lon=77.6101 + index * 0.00002,
                gps_accuracy_m=9.0,
                text=text,
                source="api",
            )
        )
        assert accepted.accepted

    # The stale-view ordering: all perception, then all dedup, then synthesis.
    perception.run_once(count=10)
    dedup.run_once(count=10)

    linked = bus.messages("reports.linked")
    assert len(linked) == 4
    assert all(m["payload"]["decision"] == "new_incident_seed" for m in linked), (
        "fixture no longer reproduces the race: A2 saw an incident it should not have"
    )

    synthesis.run_once(count=10)

    incidents = _incidents()
    assert len(incidents) == 1
    assert incidents[0]["report_count"] == 4
    assert incidents[0]["distinct_reporters"] == 4


def test_recheck_never_merges_what_the_ordinary_path_would_refuse(clean_db: None) -> None:
    """The re-check applies the same bar as A2 and the adjudicator, so it can
    only ever turn a seed into a join — never manufacture a merge."""
    from agents.a0_intake import IntakeAgent, Submission
    from agents.a1_perception import PerceptionAgent
    from agents.a2_dedup import DedupAgent
    from agents.a3_synthesis import SynthesisAgent

    load_reference()
    bus = InMemoryBus()

    perception = PerceptionAgent(bus, consumer="far-a1", provider=_provider())
    dedup = DedupAgent(bus, consumer="far-a2")
    synthesis = SynthesisAgent(bus, consumer="far-a3")
    for agent in (perception, dedup, synthesis):
        bus.create_group(agent.input_topic, agent.group)

    intake = IntakeAgent(bus)
    # Same category, ~1.5 km apart: far outside the 75 m pothole radius.
    for index, (lat, lon) in enumerate([(12.9345, 77.6101), (12.9235, 77.6221)]):
        intake.intake(
            Submission(
                device_hash=f"far-device-{index}",
                lat=lat,
                lon=lon,
                gps_accuracy_m=9.0,
                text="Deep pothole on the road here",
                source="api",
            )
        )

    perception.run_once(count=10)
    dedup.run_once(count=10)
    synthesis.run_once(count=10)

    assert len(_incidents()) == 2
