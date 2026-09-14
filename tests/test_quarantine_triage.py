"""Quarantine triage (PRD section 8.2, P4).

A surface for reviewing and releasing blocked envelopes: `GET /v1/quarantine`,
`GET /v1/quarantine/{id}`, `POST /v1/quarantine/{id}/release`, and
`POST /v1/quarantine/{id}/discard`. Runs the real stack (Postgres + Redis),
same as `tests/test_pipeline_m0.py` — skipped when the compose stack is down.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.av_sentinel.agent import Sentinel
from bus.redis import RedisBus
from common.db import connect
from scripts.emit_report import build
from tests.conftest import requires_postgres, requires_redis

pytestmark = [pytest.mark.integration, requires_postgres, requires_redis]

INGESTED = "reports.ingested"


@pytest.fixture
def sentinel(clean_db: None, redis_bus: RedisBus) -> Sentinel:
    s = Sentinel(redis_bus, consumer="sentinel-quarantine-test", topics=(INGESTED,))
    s.subscribe_all()
    return s


@pytest.fixture
def client() -> TestClient:
    from api.main import app

    return TestClient(app)


def _quarantine_one(sentinel: Sentinel, bus: RedisBus) -> tuple[str, str]:
    """Publish a structurally broken envelope and let Sentinel quarantine it.

    Returns (trace_id, quarantine_id).
    """
    envelope = build(12.9345, 77.6101, "pothole", "sha256:test").to_dict()
    envelope["confidence"] = 42.0  # outside [0,1]: guaranteed L1 fail_hard
    trace_id = str(envelope["trace_id"])
    bus.publish(INGESTED, envelope)
    sentinel.run_once()

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT quarantine_id FROM quarantine WHERE trace_id = %s", (trace_id,))
        row = cur.fetchone()
    assert row is not None
    return trace_id, str(row["quarantine_id"])


# -- listing ---------------------------------------------------------------


def test_quarantined_envelope_appears_in_the_pending_list(
    sentinel: Sentinel, redis_bus: RedisBus, client: TestClient
) -> None:
    _trace_id, quarantine_id = _quarantine_one(sentinel, redis_bus)

    response = client.get("/v1/quarantine")

    assert response.status_code == 200
    ids = {row["quarantine_id"] for row in response.json()}
    assert quarantine_id in ids


def test_quarantine_detail_carries_the_full_envelope(
    sentinel: Sentinel, redis_bus: RedisBus, client: TestClient
) -> None:
    _trace_id, quarantine_id = _quarantine_one(sentinel, redis_bus)

    response = client.get(f"/v1/quarantine/{quarantine_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["topic"] == INGESTED
    assert body["envelope"]["topic"] == INGESTED
    assert body["reasons"]


def test_unknown_quarantine_id_is_404(client: TestClient, clean_db: None) -> None:
    response = client.get("/v1/quarantine/00000000-0000-7000-8000-00000000dead")
    assert response.status_code == 404


# -- release -----------------------------------------------------------


def test_release_republishes_to_the_original_topic(
    sentinel: Sentinel, redis_bus: RedisBus, client: TestClient
) -> None:
    _trace_id, quarantine_id = _quarantine_one(sentinel, redis_bus)
    before = redis_bus.length(INGESTED)

    response = client.post(
        f"/v1/quarantine/{quarantine_id}/release", json={"reviewed_by": "ops:jane"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "released"
    assert body["republished_topic"] == INGESTED
    assert redis_bus.length(INGESTED) == before + 1


def test_released_envelope_is_marked_reviewed(
    sentinel: Sentinel, redis_bus: RedisBus, client: TestClient
) -> None:
    _trace_id, quarantine_id = _quarantine_one(sentinel, redis_bus)
    client.post(f"/v1/quarantine/{quarantine_id}/release", json={"reviewed_by": "ops:jane"})

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, reviewed_by, reviewed_at FROM quarantine WHERE quarantine_id = %s",
            (quarantine_id,),
        )
        row = cur.fetchone()

    assert row is not None
    assert row["status"] == "released"
    assert row["reviewed_by"] == "ops:jane"
    assert row["reviewed_at"] is not None


def test_releasing_twice_is_rejected(
    sentinel: Sentinel, redis_bus: RedisBus, client: TestClient
) -> None:
    _trace_id, quarantine_id = _quarantine_one(sentinel, redis_bus)
    first = client.post(f"/v1/quarantine/{quarantine_id}/release", json={"reviewed_by": "a"})
    assert first.status_code == 200

    second = client.post(f"/v1/quarantine/{quarantine_id}/release", json={"reviewed_by": "b"})
    assert second.status_code == 409


# -- discard -----------------------------------------------------------


def test_discard_does_not_republish(
    sentinel: Sentinel, redis_bus: RedisBus, client: TestClient
) -> None:
    _trace_id, quarantine_id = _quarantine_one(sentinel, redis_bus)
    before = redis_bus.length(INGESTED)

    response = client.post(
        f"/v1/quarantine/{quarantine_id}/discard", json={"reviewed_by": "ops:jane"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "discarded"
    assert redis_bus.length(INGESTED) == before


def test_discarded_envelope_leaves_the_pending_list(
    sentinel: Sentinel, redis_bus: RedisBus, client: TestClient
) -> None:
    _trace_id, quarantine_id = _quarantine_one(sentinel, redis_bus)
    client.post(f"/v1/quarantine/{quarantine_id}/discard", json={"reviewed_by": "ops:jane"})

    response = client.get("/v1/quarantine")

    ids = {row["quarantine_id"] for row in response.json()}
    assert quarantine_id not in ids
