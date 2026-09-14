"""A0 — Intake / Gateway (PRD section 7/A0).

Covers the validation rules, the ward join and the rate limiter. Media
handling is in test_media.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.a0_intake import RATE_LIMIT, IntakeAgent, Submission
from agents.av_sentinel.layers.structural import verify_structural
from bus.memory import InMemoryBus
from common.db import connect
from scripts.load_reference_data import load as load_reference
from tests.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

# Inside the Koramangala 5th Block fixture ward.
IN_WARD = (12.9345, 77.6101)
# Inside the city polygon but outside every fixture ward.
BETWEEN_WARDS = (12.9500, 77.6100)
# Outside the city polygon entirely.
OUTSIDE_CITY = (13.9821, 77.1044)


@pytest.fixture
def intake(clean_db: None, memory_bus: InMemoryBus) -> IntakeAgent:
    load_reference()
    return IntakeAgent(memory_bus)


def _submission(**overrides: Any) -> Submission:
    defaults: dict[str, Any] = {
        "device_hash": "sha256:test-device",
        "lat": IN_WARD[0],
        "lon": IN_WARD[1],
        "gps_accuracy_m": 8.0,
        "text": "Huge pothole outside the school gate",
        "language_hint": "en",
        "source": "api",
    }
    defaults.update(overrides)
    return Submission(**defaults)


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


# -- acceptance ----------------------------------------------------------


def test_valid_submission_is_accepted(intake: IntakeAgent) -> None:
    result = intake.intake(_submission())

    assert result.accepted
    assert result.report_id is not None
    assert result.trace_id


def test_acceptance_publishes_reports_ingested(
    intake: IntakeAgent, memory_bus: InMemoryBus
) -> None:
    intake.intake(_submission())
    assert memory_bus.length("reports.ingested") == 1


def test_published_event_passes_sentinel_l1(intake: IntakeAgent, memory_bus: InMemoryBus) -> None:
    intake.intake(_submission())
    envelope = memory_bus.messages("reports.ingested")[0]
    assert verify_structural(envelope).passed, verify_structural(envelope).reasons


def test_report_row_is_persisted(intake: IntakeAgent) -> None:
    result = intake.intake(_submission())
    rows = _rows("SELECT status, raw_text FROM reports WHERE report_id = %s", (result.report_id,))
    assert rows[0]["status"] == "ingested"
    assert rows[0]["raw_text"] == "Huge pothole outside the school gate"


def test_a0_leaves_the_perception_columns_empty(intake: IntakeAgent) -> None:
    """A0 owns the row; A1 owns those columns. A0 filling them would be one
    agent writing another's fields (PRD section 7)."""
    result = intake.intake(_submission())
    row = _rows(
        "SELECT category, severity_raw, summary, embedding FROM reports WHERE report_id = %s",
        (result.report_id,),
    )[0]
    assert row == {"category": None, "severity_raw": None, "summary": None, "embedding": None}


def test_intake_originates_a_trace(intake: IntakeAgent, memory_bus: InMemoryBus) -> None:
    """A0 is the trace boundary: nothing caused this message."""
    intake.intake(_submission())
    envelope = memory_bus.messages("reports.ingested")[0]
    assert envelope["causation_id"] is None


# -- the ward join -------------------------------------------------------


def test_ward_is_resolved_by_polygon_join(intake: IntakeAgent) -> None:
    assert intake.intake(_submission()).ward_id == "BLR-151"


def test_point_outside_every_ward_gets_no_ward(intake: IntakeAgent) -> None:
    """None is a real answer. The fixture geometry covers part of the city, and
    a report between wards must not be assigned to a neighbour."""
    result = intake.intake(_submission(lat=BETWEEN_WARDS[0], lon=BETWEEN_WARDS[1]))

    assert result.accepted
    assert result.ward_id is None


# -- rejection -----------------------------------------------------------


def test_location_outside_the_city_is_rejected(intake: IntakeAgent) -> None:
    """PRD section 8.1, A0 invariant."""
    result = intake.intake(_submission(lat=OUTSIDE_CITY[0], lon=OUTSIDE_CITY[1]))

    assert not result.accepted
    assert result.reason_code == "outside_city_boundary"


def test_missing_location_is_rejected(intake: IntakeAgent) -> None:
    result = intake.intake(_submission(lat=None, lon=None))

    assert not result.accepted
    assert result.reason_code == "missing_location"


def test_submission_with_no_content_is_rejected(intake: IntakeAgent) -> None:
    result = intake.intake(_submission(text=None))

    assert not result.accepted
    assert result.reason_code == "malformed_request"


def test_useless_gps_accuracy_is_rejected(intake: IntakeAgent) -> None:
    """A fix accurate to 5 km cannot be assigned to a ward, and pretending
    otherwise would corrupt every downstream distance calculation."""
    result = intake.intake(_submission(gps_accuracy_m=5000.0))

    assert not result.accepted
    assert result.reason_code == "missing_location"


def test_rejection_publishes_reports_rejected(intake: IntakeAgent, memory_bus: InMemoryBus) -> None:
    """A rejection is auditable too."""
    intake.intake(_submission(lat=OUTSIDE_CITY[0], lon=OUTSIDE_CITY[1]))

    assert memory_bus.length("reports.rejected") == 1
    assert memory_bus.length("reports.ingested") == 0


def test_rejection_event_passes_sentinel_l1(intake: IntakeAgent, memory_bus: InMemoryBus) -> None:
    intake.intake(_submission(lat=OUTSIDE_CITY[0], lon=OUTSIDE_CITY[1]))
    envelope = memory_bus.messages("reports.rejected")[0]
    assert verify_structural(envelope).passed


def test_rejection_writes_no_report_row(intake: IntakeAgent) -> None:
    intake.intake(_submission(lat=OUTSIDE_CITY[0], lon=OUTSIDE_CITY[1]))
    assert _rows("SELECT 1 FROM reports") == []


def test_rejection_reason_reaches_the_payload(intake: IntakeAgent, memory_bus: InMemoryBus) -> None:
    intake.intake(_submission(lat=OUTSIDE_CITY[0], lon=OUTSIDE_CITY[1]))
    payload = memory_bus.messages("reports.rejected")[0]["payload"]

    assert payload["reason_code"] == "outside_city_boundary"
    assert "outside the city boundary" in payload["reason"]


# -- rate limiting -------------------------------------------------------


def test_device_is_rate_limited(intake: IntakeAgent) -> None:
    """PRD section 7/A0: rate-limit per device to blunt spam and brigading."""
    for _ in range(RATE_LIMIT):
        assert intake.intake(_submission()).accepted

    blocked = intake.intake(_submission())

    assert not blocked.accepted
    assert blocked.reason_code == "rate_limited"


def test_rate_limit_counts_rejected_attempts_too(intake: IntakeAgent) -> None:
    """Counting only successful reports would let a flood of malformed ones
    sail past the limit."""
    for _ in range(RATE_LIMIT):
        intake.intake(_submission(lat=OUTSIDE_CITY[0], lon=OUTSIDE_CITY[1]))

    blocked = intake.intake(_submission())

    assert not blocked.accepted
    assert blocked.reason_code == "rate_limited"


def test_rate_limit_is_per_device(intake: IntakeAgent) -> None:
    for _ in range(RATE_LIMIT):
        intake.intake(_submission(device_hash="sha256:noisy"))

    assert intake.intake(_submission(device_hash="sha256:quiet")).accepted


def test_every_attempt_is_recorded(intake: IntakeAgent) -> None:
    intake.intake(_submission())
    intake.intake(_submission(lat=OUTSIDE_CITY[0], lon=OUTSIDE_CITY[1]))

    outcomes = {row["outcome"] for row in _rows("SELECT outcome FROM intake_attempts")}
    assert outcomes == {"accepted", "rejected"}
