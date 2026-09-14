"""CivicAI API (PRD section 12).

P1 ships the read surface that M0 needs: the trace viewer and agent health.
The citizen and dashboard write endpoints belong to the agents that own their
tables (A0 in P2, A6 in P5) and arrive with them — an endpoint that accepted a
report today would have nothing behind it but a row insert dressed up as
intake.

Implemented here:

    GET /v1/health              liveness plus dependency status
    GET /v1/health/agents       per-agent liveness, lag and verdict mix
    GET /v1/trace/{trace_id}    full agent decision DAG with Sentinel verdicts
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agents.av_sentinel.agent import GROUP as SENTINEL_GROUP
from agents.av_sentinel.agent import VERDICT_TOPIC, VERIFIED_TOPICS
from api import queries
from bus.factory import make_bus
from common import db
from common.config import settings
from common.logging import configure_logging, get_logger

log = get_logger(__name__)

app = FastAPI(
    title="CivicAI",
    version="0.1.0",
    summary="Urban Complaint Intelligence & Response System",
    description=(
        "P1 read surface: trace viewer and agent health. "
        "Citizen and dashboard endpoints arrive with the agents that own them."
    ),
)


@app.on_event("startup")
def _startup() -> None:
    configure_logging()
    log.info(
        "api starting",
        extra={"env": settings().env, "sentinel_mode": settings().sentinel_mode},
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DependencyHealth(BaseModel):
    postgres: bool
    redis: bool


class Health(BaseModel):
    status: str
    env: str
    sentinel_mode: str
    dependencies: DependencyHealth


class TopicLag(BaseModel):
    topic: str
    pending: int = Field(description="Entries claimed but not yet acked.")
    lag: int | None = Field(default=None, description="Entries never delivered to the group.")


class AgentHealth(BaseModel):
    agent: str
    version: str | None = None
    last_seen_at: dt.datetime | None = None
    runs: int = 0
    errors: int = 0
    skips: int = 0
    in_flight: int = 0
    verdicts: dict[str, int] = Field(default_factory=dict)


class AgentsHealth(BaseModel):
    window_hours: int
    sentinel_mode: str
    sentinel_layers_active: list[str] = Field(
        description="Verification layers actually running. Only L1 in P1."
    )
    sentinel_gate_enforced: bool = Field(
        description=(
            "Whether consumers wait for a verdict before acting. False in P1: "
            "Sentinel verifies alongside consumers rather than in front of them, "
            "so the configured mode is aspirational until the gate lands in P4."
        )
    )
    quarantine_pending: int
    agents: list[AgentHealth]
    sentinel_lag: list[TopicLag]


class TraceNode(BaseModel):
    message_id: str
    causation_id: str | None
    correlation_id: str
    topic: str
    schema_version: str
    emitted_at: dt.datetime
    producer: dict[str, Any]
    confidence: float
    rationale: str
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)


class TraceEdge(BaseModel):
    source: str = Field(description="causation_id: the message that caused the target.")
    target: str


class Trace(BaseModel):
    trace_id: str
    message_count: int = Field(description="Nodes in the graph, excluding Sentinel verdicts.")
    verdict_count: int = Field(description="Sentinel verdicts recorded across the trace.")
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    roots: list[str] = Field(description="Messages with no cause inside this trace.")
    nodes: list[TraceNode]
    edges: list[TraceEdge]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/v1/health", response_model=Health, tags=["health"])
def health() -> Health:
    """Liveness plus dependency status. Never raises — it reports."""
    bus = make_bus()
    redis_ok = bool(getattr(bus, "healthy", lambda: True)())
    postgres_ok = db.healthy()

    return Health(
        status="ok" if (redis_ok and postgres_ok) else "degraded",
        env=settings().env,
        sentinel_mode=settings().sentinel_mode,
        dependencies=DependencyHealth(postgres=postgres_ok, redis=redis_ok),
    )


@app.get("/v1/health/agents", response_model=AgentsHealth, tags=["health"])
def agents_health(window_hours: int = 24) -> AgentsHealth:
    """Per-agent liveness, lag and verdict mix (PRD section 12)."""
    window = dt.timedelta(hours=window_hours)
    activity = queries.agent_activity(window)
    mix = queries.verdict_mix(window)

    agents = [
        AgentHealth(
            agent=str(row["agent"]),
            version=row.get("version"),
            last_seen_at=row.get("last_seen_at"),
            runs=int(row["runs"]),
            errors=int(row["errors"]),
            skips=int(row["skips"]),
            in_flight=int(row["in_flight"]),
            verdicts=mix.get(str(row["agent"]), {}),
        )
        for row in activity
    ]

    return AgentsHealth(
        window_hours=window_hours,
        sentinel_mode=settings().sentinel_mode,
        # Reported honestly: only L1 runs in P1. L2/L3/L4 arrive in P4 and P6,
        # and nothing yet blocks a consumer from acting before a verdict lands.
        sentinel_layers_active=["L1"],
        sentinel_gate_enforced=False,
        quarantine_pending=queries.quarantine_depth(),
        agents=agents,
        sentinel_lag=_sentinel_lag(),
    )


@app.get("/v1/trace/{trace_id}", response_model=Trace, tags=["trace"])
def trace(trace_id: str) -> Trace:
    """The full agent decision DAG for one trace, with Sentinel verdicts.

    This is what makes any incident replayable back to the original citizen
    submission (PRD section 9.2): every message, what caused it, which agent
    produced it with what confidence, and how Sentinel judged it.
    """
    archived = queries.trace_messages(trace_id)
    if not archived:
        raise HTTPException(status_code=404, detail=f"no trace {trace_id}")

    # Sentinel's verdicts derive from the message they judge, so they share its
    # trace_id and would otherwise double every node in the graph. They are
    # surfaced on the node they are about, under `verdicts`, which is what PRD
    # section 12 asks for: the agent decision DAG *plus* its verdicts.
    # Quarantine copies stay visible — a blocked message is a real event.
    messages = [row for row in archived if row["topic"] != VERDICT_TOPIC]

    verdicts_by_message: dict[str, list[dict[str, Any]]] = {}
    for verdict in queries.trace_verdicts(trace_id):
        verdicts_by_message.setdefault(str(verdict["message_id"]), []).append(
            {
                "verdict_id": str(verdict["verdict_id"]),
                "layer": verdict["layer"],
                "verdict": verdict["verdict"],
                "reasons": verdict["reasons"],
                "judge_model": verdict["judge_model"],
                "created_at": verdict["created_at"],
            }
        )

    runs_by_message: dict[str, list[dict[str, Any]]] = {}
    for run in queries.trace_runs(trace_id):
        runs_by_message.setdefault(str(run["message_id"]), []).append(
            {
                "run_id": str(run["run_id"]),
                "agent": run["agent"],
                "version": run["version"],
                "outcome": run["outcome"],
                "started_at": run["started_at"],
                "ended_at": run["ended_at"],
                "error": run["error"],
                "delivery_count": run["delivery_count"],
            }
        )

    present = {str(row["message_id"]) for row in messages}
    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    roots: list[str] = []

    for row in messages:
        message_id = str(row["message_id"])
        causation_id = str(row["causation_id"]) if row["causation_id"] else None

        nodes.append(
            TraceNode(
                message_id=message_id,
                causation_id=causation_id,
                correlation_id=str(row["correlation_id"]),
                topic=str(row["topic"]),
                schema_version=str(row["schema_version"]),
                emitted_at=row["emitted_at"],
                producer={
                    "agent": row["producer_agent"],
                    "version": row["producer_version"],
                    "model": row["producer_model"],
                },
                confidence=float(row["confidence"]),
                rationale=str(row["rationale"]),
                verdicts=verdicts_by_message.get(message_id, []),
                runs=runs_by_message.get(message_id, []),
            )
        )

        # An edge whose source is outside this trace would dangle in the viewer;
        # such a node is a root as far as this trace is concerned.
        if causation_id and causation_id in present:
            edges.append(TraceEdge(source=causation_id, target=message_id))
        else:
            roots.append(message_id)

    timestamps = [row["emitted_at"] for row in archived]

    return Trace(
        trace_id=trace_id,
        message_count=len(nodes),
        verdict_count=sum(len(v) for v in verdicts_by_message.values()),
        started_at=min(timestamps),
        ended_at=max(timestamps),
        roots=roots,
        nodes=nodes,
        edges=edges,
    )


def _sentinel_lag() -> list[TopicLag]:
    """Sentinel's backlog per topic, when the bus can report it."""
    bus = make_bus()
    group_info = getattr(bus, "group_info", None)
    if group_info is None:
        return []

    lag: list[TopicLag] = []
    for topic in VERIFIED_TOPICS:
        info = group_info(topic, SENTINEL_GROUP)
        if not info:
            continue
        raw_lag = info.get("lag")
        lag.append(
            TopicLag(
                topic=topic,
                pending=int(info.get("pending", 0)),
                lag=int(raw_lag) if raw_lag is not None else None,
            )
        )
    return lag
