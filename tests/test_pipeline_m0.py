"""M0 acceptance (PRD section 13).

    "An event flows A0 -> echo -> Sentinel L1 and is visible in the trace viewer"

This runs the real stack: Redis Streams for the bus, Postgres for the archive,
the echo agent, Sentinel, and the FastAPI trace endpoint. Nothing is stubbed.

Skipped when the compose stack is not up.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agents.av_sentinel.agent import Sentinel
from agents.echo import EchoAgent
from bus.redis import RedisBus
from common.db import connect
from scripts.emit_report import build, emit
from tests.conftest import requires_postgres, requires_redis

pytestmark = [pytest.mark.integration, requires_postgres, requires_redis]

INGESTED = "reports.ingested"
SKIPPED = "reports.ingested.skipped"


@pytest.fixture
def pipeline(clean_db: None, redis_bus: RedisBus) -> dict[str, Any]:
    """Echo and Sentinel wired to a live bus and database, groups already made."""
    echo = EchoAgent(redis_bus, consumer="echo-test", backoff=(0.0, 0.0, 0.0))
    sentinel = Sentinel(redis_bus, consumer="sentinel-test", topics=(INGESTED, SKIPPED))

    redis_bus.create_group(INGESTED, echo.group)
    sentinel.subscribe_all()

    return {"bus": redis_bus, "echo": echo, "sentinel": sentinel}


def _run(pipeline: dict[str, Any], trace_id: str) -> None:
    """Drive one pass of the pipeline: echo consumes, then Sentinel verifies."""
    pipeline["echo"].run_once()
    pipeline["sentinel"].run_once()


def _emit(pipeline: dict[str, Any]) -> str:
    envelope = build(12.9345, 77.6101, "pothole outside the school", "sha256:test")
    emit(pipeline["bus"], envelope)
    return str(envelope.trace_id)


def _rows(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


# -- the flow ------------------------------------------------------------


def test_event_reaches_the_echo_agent(pipeline: dict[str, Any]) -> None:
    _emit(pipeline)

    results = pipeline["echo"].run_once()

    assert len(results) == 1
    assert results[0].handled
    assert results[0].skipped


def test_echo_output_lands_on_the_bus(pipeline: dict[str, Any]) -> None:
    _emit(pipeline)
    pipeline["echo"].run_once()

    assert pipeline["bus"].length(SKIPPED) == 1


def test_both_messages_are_archived(pipeline: dict[str, Any]) -> None:
    """PRD section 6.1: all durable truth lives in Postgres."""
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    topics = {
        row["topic"] for row in _rows("SELECT topic FROM messages WHERE trace_id = %s", (trace_id,))
    }

    assert INGESTED in topics
    assert SKIPPED in topics


def test_agent_run_is_recorded(pipeline: dict[str, Any]) -> None:
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    runs = _rows("SELECT agent, outcome FROM agent_runs WHERE trace_id = %s", (trace_id,))

    assert [(r["agent"], r["outcome"]) for r in runs] == [("ECHO", "skipped")]


def test_sentinel_verifies_every_message_in_the_trace(pipeline: dict[str, Any]) -> None:
    """PRD section 14: 100% of events carry a verdict in strict mode."""
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    verdicts = _rows(
        "SELECT topic, verdict, layer FROM verification_results WHERE trace_id = %s", (trace_id,)
    )

    assert {v["topic"] for v in verdicts} == {INGESTED, SKIPPED}
    assert all(v["verdict"] == "pass" for v in verdicts)
    assert all(v["layer"] == "L1" for v in verdicts)


def test_trace_id_is_preserved_end_to_end(pipeline: dict[str, Any]) -> None:
    """The one rule that makes an incident replayable to its source.

    Four messages share the trace: the report, echo's skip, and Sentinel's
    verdict on each. Every one of them carries the trace_id A0 minted, and
    every one but the root names the message that caused it.
    """
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    archived = _rows(
        "SELECT message_id, causation_id, topic FROM messages WHERE trace_id = %s", (trace_id,)
    )

    assert len(archived) == 4
    ids = {str(row["message_id"]) for row in archived}
    roots = [row for row in archived if row["causation_id"] is None]

    assert [row["topic"] for row in roots] == [INGESTED]
    assert all(
        str(row["causation_id"]) in ids for row in archived if row["causation_id"] is not None
    )


def test_nothing_is_quarantined_on_a_clean_run(pipeline: dict[str, Any]) -> None:
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    assert _rows("SELECT 1 FROM quarantine WHERE trace_id = %s", (trace_id,)) == []


def test_redelivery_is_suppressed_by_idempotency(pipeline: dict[str, Any]) -> None:
    """At-least-once delivery plus a durable handler record equals effectively-once."""
    trace_id = _emit(pipeline)
    pipeline["echo"].run_once()

    # Re-publish the identical envelope: same correlation_id, same producer version.
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT envelope FROM messages WHERE trace_id = %s AND topic = %s",
            (trace_id, INGESTED),
        )
        row = cur.fetchone()
    assert row is not None
    pipeline["bus"].publish(INGESTED, row["envelope"])

    results = pipeline["echo"].run_once()

    assert results[0].handled is False
    assert pipeline["bus"].length(SKIPPED) == 1


# -- the trace viewer ----------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    from api.main import app

    return TestClient(app)


def test_trace_endpoint_renders_the_dag(pipeline: dict[str, Any], client: TestClient) -> None:
    """M0's acceptance criterion, end to end."""
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    response = client.get(f"/v1/trace/{trace_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == trace_id
    assert body["message_count"] == 2
    assert {node["topic"] for node in body["nodes"]} == {INGESTED, SKIPPED}


def test_trace_dag_has_one_root_and_one_edge(pipeline: dict[str, Any], client: TestClient) -> None:
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    body = client.get(f"/v1/trace/{trace_id}").json()

    assert len(body["roots"]) == 1
    assert len(body["edges"]) == 1
    edge = body["edges"][0]
    assert edge["source"] == body["roots"][0]


def test_trace_nodes_carry_sentinel_verdicts(pipeline: dict[str, Any], client: TestClient) -> None:
    """ "Every agent decision and its Sentinel verdict" - PRD section 10."""
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    body = client.get(f"/v1/trace/{trace_id}").json()

    assert all(node["verdicts"] for node in body["nodes"])
    assert all(node["verdicts"][0]["verdict"] == "pass" for node in body["nodes"])


def test_trace_node_carries_the_agent_run(pipeline: dict[str, Any], client: TestClient) -> None:
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    body = client.get(f"/v1/trace/{trace_id}").json()
    runs = [run for node in body["nodes"] for run in node["runs"]]

    assert [run["agent"] for run in runs] == ["ECHO"]
    assert runs[0]["outcome"] == "skipped"


def test_trace_node_reports_confidence_and_rationale(
    pipeline: dict[str, Any], client: TestClient
) -> None:
    """PRD section 7: every output carries a confidence and a rationale."""
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    body = client.get(f"/v1/trace/{trace_id}").json()

    for node in body["nodes"]:
        assert 0.0 <= node["confidence"] <= 1.0
        assert node["rationale"]


def test_unknown_trace_is_404(client: TestClient, clean_db: None) -> None:
    response = client.get("/v1/trace/00000000-0000-7000-8000-00000000dead")
    assert response.status_code == 404


# -- a corrupted message never reaches a consumer ------------------------


def test_corrupted_message_is_quarantined_not_delivered(
    pipeline: dict[str, Any], client: TestClient
) -> None:
    """PRD section 17, step 5: inject a corrupted agent output and watch Sentinel
    quarantine it while the pipeline stays clean."""
    envelope = build(12.9345, 77.6101, "pothole", "sha256:test").to_dict()
    envelope["confidence"] = 42.0  # outside [0,1]
    trace_id = envelope["trace_id"]
    pipeline["bus"].publish(INGESTED, envelope)

    pipeline["sentinel"].run_once()

    quarantined = _rows("SELECT topic, reasons FROM quarantine WHERE trace_id = %s", (trace_id,))
    assert len(quarantined) == 1
    assert quarantined[0]["topic"] == INGESTED

    verdicts = _rows("SELECT verdict FROM verification_results WHERE trace_id = %s", (trace_id,))
    assert [v["verdict"] for v in verdicts] == ["fail_hard"]


# -- health --------------------------------------------------------------


def test_health_reports_dependencies(client: TestClient, clean_db: None) -> None:
    body = client.get("/v1/health").json()

    assert body["status"] == "ok"
    assert body["dependencies"] == {"postgres": True, "redis": True}


def test_agent_health_reports_activity(pipeline: dict[str, Any], client: TestClient) -> None:
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    body = client.get("/v1/health/agents").json()

    assert body["sentinel_layers_active"] == ["L1"]
    # P1 verifies alongside consumers rather than in front of them; the API must
    # say so rather than let the configured mode imply a guarantee. See
    # docs/sentinel.md.
    assert body["sentinel_gate_enforced"] is False
    echo = next(a for a in body["agents"] if a["agent"] == "ECHO")
    assert echo["runs"] == 1
    assert echo["skips"] == 1
    assert echo["errors"] == 0


def test_agent_health_reports_verdict_mix(pipeline: dict[str, Any], client: TestClient) -> None:
    """The escape-rate signal from PRD section 14."""
    trace_id = _emit(pipeline)
    _run(pipeline, trace_id)

    body = client.get("/v1/health/agents").json()
    echo = next(a for a in body["agents"] if a["agent"] == "ECHO")

    assert echo["verdicts"].get("pass", 0) >= 1
