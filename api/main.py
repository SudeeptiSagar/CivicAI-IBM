"""CivicAI API (PRD section 12).

    POST /v1/reports            citizen submission (multipart) -> A0
    GET  /v1/reports/{id}       status and linked incident
    GET  /v1/health             liveness plus dependency status
    GET  /v1/health/agents      per-agent liveness, lag and verdict mix
    GET  /v1/trace/{trace_id}   full agent decision DAG with Sentinel verdicts

    GET  /v1/incidents          department queue: filter + priority order
    GET  /v1/incidents/{id}     one incident's full record

    GET  /v1/quarantine           blocked envelopes awaiting human triage
    GET  /v1/quarantine/{id}      one blocked envelope's full record
    POST /v1/quarantine/{id}/release   re-publish to its original topic
    POST /v1/quarantine/{id}/discard   close it without re-publishing

Still to come, with the agents that own the tables behind them:
`/v1/super-incidents` and the resolve/confirm endpoints (P5). An endpoint
whose agent does not exist would be a row insert dressed up as a decision.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from agents.a0_intake import IntakeAgent, Submission
from agents.av_sentinel.agent import GROUP as SENTINEL_GROUP
from agents.av_sentinel.agent import VERDICT_TOPIC, VERIFIED_TOPICS
from api import queries
from bus.factory import make_bus
from common import db, storage
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
    object_store: bool


class Health(BaseModel):
    status: str
    env: str
    sentinel_mode: str
    dependencies: DependencyHealth


class ReportAccepted(BaseModel):
    report_id: str
    trace_id: str
    ward_id: str | None = Field(
        default=None, description="Null when the point falls outside every loaded ward."
    )
    media_keys: list[str] = Field(default_factory=list)
    matched_incident: str | None = Field(
        default=None,
        description=(
            "Always null at intake: dedup runs downstream. Poll "
            "GET /v1/reports/{id} once A2 has linked the report."
        ),
    )


class ReportStatus(BaseModel):
    report_id: str
    trace_id: str
    status: str
    created_at: dt.datetime
    ward_id: str | None
    category: str | None = Field(default=None, description="Null until A1 has run.")
    severity_raw: int | None = None
    summary: str | None = None
    incident_id: str | None = Field(default=None, description="Null until A2 has linked it.")
    incident_report_count: int | None = Field(
        default=None, description='The "17 others reported this" figure from PRD section 5.'
    )
    incident_title: str | None = None


class FactorBreakdownItem(BaseModel):
    factor: str
    weight: float
    value: float
    contribution: float


class IncidentSummary(BaseModel):
    incident_id: str
    title: str
    category: str
    ward_id: str | None
    lat: float
    lon: float
    first_reported_at: dt.datetime
    last_reported_at: dt.datetime
    report_count: int
    distinct_reporters: int
    priority_score: float | None
    priority_band: str | None = Field(default=None, description="Null until A4 has scored it.")
    department_id: str | None = Field(default=None, description="Null until A5 has routed it.")
    cc_departments: list[str] = Field(default_factory=list)
    sla_due_at: dt.datetime | None
    status: str


class Incident(IncidentSummary):
    why: str | None = None
    factor_breakdown: list[FactorBreakdownItem] | None = None
    super_incident_id: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class QuarantineSummary(BaseModel):
    quarantine_id: str
    message_id: str
    trace_id: str
    topic: str
    verdict_id: str | None
    reasons: list[dict[str, Any]]
    status: str
    quarantined_at: dt.datetime
    reviewed_at: dt.datetime | None
    reviewed_by: str | None


class QuarantineDetail(QuarantineSummary):
    envelope: dict[str, Any] = Field(description="The full blocked envelope, as Sentinel saw it.")


class QuarantineReview(BaseModel):
    reviewed_by: str = Field(description="Who is releasing or discarding this envelope.")
    note: str | None = None


class QuarantineReleased(BaseModel):
    quarantine_id: str
    status: str
    republished_topic: str
    republished_message_id: str


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
        description="Verification layers actually running. L1 and L2 as of P4; L3/L4 are P6."
    )
    sentinel_gate_enforced: bool = Field(
        description=(
            "Whether consumers wait for a verdict before acting "
            "(Settings.sentinel_gate_enabled, common/verdict_gate.py). Off by "
            "default even in P4, so this reports the actually-configured state "
            "rather than implying the gate is always on just because it exists."
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


@app.post("/v1/reports", response_model=ReportAccepted, status_code=202, tags=["citizen"])
def submit_report(
    device_hash: Annotated[str, Form()],
    lat: Annotated[float | None, Form()] = None,
    lon: Annotated[float | None, Form()] = None,
    gps_accuracy_m: Annotated[float | None, Form()] = None,
    text: Annotated[str | None, Form()] = None,
    language_hint: Annotated[str | None, Form()] = None,
    photo: Annotated[UploadFile | None, File()] = None,
    audio: Annotated[UploadFile | None, File()] = None,
) -> ReportAccepted:
    """Citizen submission (PRD section 12).

    Returns 202, not 201: A0 has accepted and published the report, but
    perception, dedup and consolidation happen downstream. `matched_incident`
    is therefore null here — poll `GET /v1/reports/{id}` for it, or follow
    `trace_id` through the trace viewer.

    A rejection is a 400 carrying the same reason code A0 published on
    `reports.rejected`, so the citizen and the audit trail agree.
    """
    submission = Submission(
        device_hash=device_hash,
        lat=lat,
        lon=lon,
        gps_accuracy_m=gps_accuracy_m,
        text=text,
        language_hint=language_hint,
        photo=_read_upload(photo),
        audio=_read_upload(audio),
        source="pwa",
    )

    result = IntakeAgent(make_bus()).intake(submission)

    if not result.accepted:
        raise HTTPException(
            status_code=429 if result.reason_code == "rate_limited" else 400,
            detail={
                "reason_code": result.reason_code,
                "reason": result.reason,
                "trace_id": result.trace_id,
            },
        )

    return ReportAccepted(
        report_id=str(result.report_id),
        trace_id=result.trace_id,
        ward_id=result.ward_id,
        media_keys=result.media_keys,
        matched_incident=None,
    )


@app.get("/v1/reports/{report_id}", response_model=ReportStatus, tags=["citizen"])
def report_status(report_id: str) -> ReportStatus:
    """Status and linked incident for one report (PRD section 12)."""
    row = queries.report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no report {report_id}")

    return ReportStatus(
        report_id=str(row["report_id"]),
        trace_id=str(row["trace_id"]),
        status=str(row["status"]),
        created_at=row["created_at"],
        ward_id=row["ward_id"],
        category=row["category"],
        severity_raw=row["severity_raw"],
        summary=row["summary"],
        incident_id=str(row["incident_id"]) if row["incident_id"] else None,
        incident_report_count=(
            int(row["incident_report_count"]) if row["incident_report_count"] else None
        ),
        incident_title=row["incident_title"],
    )


@app.get("/v1/incidents", response_model=list[IncidentSummary], tags=["incidents"])
def list_incidents(
    ward: str | None = None,
    department: str | None = None,
    band: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[IncidentSummary]:
    """Department dashboard queue (PRD section 12): filter, sorted by priority.

    Sort order matches `incidents_priority_idx`: highest `priority_score`
    first, un-prioritized incidents (score still null) sink to the bottom
    rather than sorting arbitrarily.
    """
    rows = queries.list_incidents(
        ward=ward, department=department, band=band, status=status, limit=limit
    )
    return [_incident_summary(row) for row in rows]


@app.get("/v1/incidents/{incident_id}", response_model=Incident, tags=["incidents"])
def get_incident(incident_id: str) -> Incident:
    """One incident's full record, including its explainable score."""
    row = queries.incident(incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no incident {incident_id}")

    return Incident(
        **_incident_summary(row).model_dump(),
        why=row["why"],
        factor_breakdown=(
            [FactorBreakdownItem(**item) for item in row["factor_breakdown"]]
            if row["factor_breakdown"]
            else None
        ),
        super_incident_id=str(row["super_incident_id"]) if row["super_incident_id"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@app.get("/v1/quarantine", response_model=list[QuarantineSummary], tags=["quarantine"])
def list_quarantine(
    status: str | None = "pending", topic: str | None = None, limit: int = 100
) -> list[QuarantineSummary]:
    """Blocked envelopes awaiting human triage (PRD section 8.2, P4).

    Defaults to the `pending` queue; pass `status=` (empty string is not
    accepted by FastAPI's query parsing for `None`, so pass `status=released`
    or `status=discarded` explicitly) to see envelopes already reviewed.
    """
    rows = queries.list_quarantine(status=status, topic=topic, limit=limit)
    return [_quarantine_summary(row) for row in rows]


@app.get("/v1/quarantine/{quarantine_id}", response_model=QuarantineDetail, tags=["quarantine"])
def get_quarantine(quarantine_id: str) -> QuarantineDetail:
    """One blocked envelope's full record, including the envelope itself."""
    row = queries.quarantine_envelope(quarantine_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no quarantined envelope {quarantine_id}")
    return QuarantineDetail(**_quarantine_summary(row).model_dump(), envelope=row["envelope"])


@app.post(
    "/v1/quarantine/{quarantine_id}/release",
    response_model=QuarantineReleased,
    tags=["quarantine"],
)
def release_quarantine(quarantine_id: str, review: QuarantineReview) -> QuarantineReleased:
    """Re-publish a blocked envelope to its original topic after human review.

    The envelope re-enters the bus exactly as Sentinel first saw it — Sentinel
    verifies it again on the way through, same as any other message, so a
    release does not bypass verification, it only gives the envelope another
    chance to pass (or fail again, visibly, rather than being silently forced
    through). A human reviewer decided it is fine to try; Sentinel still has
    the last word on whether a consumer acts on it.
    """
    row = queries.quarantine_envelope(quarantine_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no quarantined envelope {quarantine_id}")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409, detail=f"quarantine {quarantine_id} is already {row['status']}"
        )

    envelope = row["envelope"]
    topic = str(envelope.get("topic") or row["topic"])
    message_id = str(envelope.get("message_id", ""))

    queries.mark_quarantine_reviewed(
        quarantine_id, status="released", reviewed_by=review.reviewed_by, note=review.note
    )
    make_bus().publish(topic, envelope)
    log.info(
        "quarantine released",
        extra={"quarantine_id": quarantine_id, "topic": topic, "reviewed_by": review.reviewed_by},
    )

    return QuarantineReleased(
        quarantine_id=quarantine_id,
        status="released",
        republished_topic=topic,
        republished_message_id=message_id,
    )


@app.post(
    "/v1/quarantine/{quarantine_id}/discard",
    response_model=QuarantineSummary,
    tags=["quarantine"],
)
def discard_quarantine(quarantine_id: str, review: QuarantineReview) -> QuarantineSummary:
    """Close a blocked envelope without re-publishing it. Terminal, not a delete
    — the row and its `envelope`/`reasons` stay for audit (PRD section 12's
    trace viewer and this triage surface both read from the same table)."""
    row = queries.quarantine_envelope(quarantine_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no quarantined envelope {quarantine_id}")
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409, detail=f"quarantine {quarantine_id} is already {row['status']}"
        )

    queries.mark_quarantine_reviewed(
        quarantine_id, status="discarded", reviewed_by=review.reviewed_by, note=review.note
    )
    updated = queries.quarantine_envelope(quarantine_id)
    assert updated is not None
    return _quarantine_summary(updated)


def _quarantine_summary(row: dict[str, Any]) -> QuarantineSummary:
    return QuarantineSummary(
        quarantine_id=str(row["quarantine_id"]),
        message_id=str(row["message_id"]),
        trace_id=str(row["trace_id"]),
        topic=row["topic"],
        verdict_id=str(row["verdict_id"]) if row["verdict_id"] else None,
        reasons=list(row["reasons"] or []),
        status=row["status"],
        quarantined_at=row["quarantined_at"],
        reviewed_at=row["reviewed_at"],
        reviewed_by=row["reviewed_by"],
    )


def _incident_summary(row: dict[str, Any]) -> IncidentSummary:
    return IncidentSummary(
        incident_id=str(row["incident_id"]),
        title=row["title"],
        category=row["category"],
        ward_id=row["ward_id"],
        lat=float(row["lat"]),
        lon=float(row["lon"]),
        first_reported_at=row["first_reported_at"],
        last_reported_at=row["last_reported_at"],
        report_count=int(row["report_count"]),
        distinct_reporters=int(row["distinct_reporters"]),
        priority_score=float(row["priority_score"]) if row["priority_score"] is not None else None,
        priority_band=row["priority_band"],
        department_id=row["department_id"],
        cc_departments=list(row["cc_departments"] or []),
        sla_due_at=row["sla_due_at"],
        status=row["status"],
    )


def _read_upload(upload: UploadFile | None) -> tuple[bytes, str] | None:
    """Read an upload into memory.

    Media is capped at 10 MB by `common.storage`, so buffering is bounded. A
    declared content type is not trusted — `store_image` re-decodes the bytes.
    """
    if upload is None or not upload.filename:
        return None
    return upload.file.read(), upload.content_type or "application/octet-stream"


@app.get("/v1/health", response_model=Health, tags=["health"])
def health() -> Health:
    """Liveness plus dependency status. Never raises — it reports."""
    bus = make_bus()
    redis_ok = bool(getattr(bus, "healthy", lambda: True)())
    postgres_ok = db.healthy()
    blobs_ok = storage.healthy()

    return Health(
        status="ok" if (redis_ok and postgres_ok and blobs_ok) else "degraded",
        env=settings().env,
        sentinel_mode=settings().sentinel_mode,
        dependencies=DependencyHealth(postgres=postgres_ok, redis=redis_ok, object_store=blobs_ok),
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
        # Reported honestly: L1 and L2 run as of P4; L3/L4 are P6. The gate
        # reflects the actual flag, not merely its existence in this build.
        sentinel_layers_active=["L1", "L2"],
        sentinel_gate_enforced=settings().sentinel_gate_enabled,
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
