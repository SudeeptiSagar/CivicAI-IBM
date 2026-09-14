"""Read queries behind the API, plus quarantine triage (P4).

Mostly read-only by construction: the API surface in PRD section 12 that
mutates business data (report submission, resolution claims) belongs to the
agents that own those tables and arrives with them, in P2 and P5.
`mark_quarantine_reviewed` is the one write here, and it is deliberately not
one of those: `quarantine` is Sentinel's own table (PRD section 8.2), not
business data, and marking a row reviewed is a human triage decision, not an
agent decision — nothing here writes to `reports`, `incidents`, or any table
an agent owns.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from common.db import connect, transaction

__all__ = [
    "agent_activity",
    "incident",
    "list_incidents",
    "list_quarantine",
    "mark_quarantine_reviewed",
    "quarantine_envelope",
    "trace_messages",
    "trace_runs",
    "trace_verdicts",
    "verdict_mix",
]


def trace_messages(trace_id: str) -> list[dict[str, Any]]:
    """Every archived message in a trace, oldest first."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT message_id, causation_id, correlation_id, topic, schema_version,
                   emitted_at, producer_agent, producer_version, producer_model,
                   confidence, rationale
            FROM messages
            WHERE trace_id = %s
            ORDER BY emitted_at, message_id
            """,
            (trace_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def trace_runs(trace_id: str) -> list[dict[str, Any]]:
    """Every agent run in a trace."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, agent, version, message_id, started_at, ended_at,
                   outcome, error, delivery_count, tokens_in, tokens_out, cost_estimate
            FROM agent_runs
            WHERE trace_id = %s
            ORDER BY started_at, run_id
            """,
            (trace_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def trace_verdicts(trace_id: str) -> list[dict[str, Any]]:
    """Every Sentinel verdict in a trace."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT verdict_id, message_id, topic, agent, layer, verdict, reasons,
                   judge_model, created_at
            FROM verification_results
            WHERE trace_id = %s
            ORDER BY created_at, verdict_id
            """,
            (trace_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def agent_activity(window: dt.timedelta = dt.timedelta(hours=24)) -> list[dict[str, Any]]:
    """Per-agent run counts and liveness over a trailing window."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT agent,
                   max(version)                                       AS version,
                   max(started_at)                                    AS last_seen_at,
                   count(*)                                           AS runs,
                   count(*) FILTER (WHERE outcome = 'error')          AS errors,
                   count(*) FILTER (WHERE outcome = 'skipped')        AS skips,
                   count(*) FILTER (WHERE outcome = 'running')        AS in_flight
            FROM agent_runs
            WHERE started_at >= now() - %s
            GROUP BY agent
            ORDER BY agent
            """,
            (window,),
        )
        return [dict(row) for row in cur.fetchall()]


def verdict_mix(window: dt.timedelta = dt.timedelta(hours=24)) -> dict[str, dict[str, int]]:
    """Verdict counts per producing agent — the escape-rate signal (PRD 14)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT agent, verdict, count(*) AS n
            FROM verification_results
            WHERE created_at >= now() - %s
            GROUP BY agent, verdict
            """,
            (window,),
        )
        mix: dict[str, dict[str, int]] = {}
        for row in cur.fetchall():
            mix.setdefault(str(row["agent"]), {})[str(row["verdict"])] = int(row["n"])
        return mix


def report(report_id: str) -> dict[str, Any] | None:
    """One report with its incident context, or None.

    The incident join is what lets a citizen be told "17 others reported this;
    it is incident #1042" instead of being handed an orphan ticket
    (PRD section 5).
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.report_id, r.trace_id, r.status, r.created_at, r.ward_id,
                   r.category, r.severity_raw, r.summary, r.incident_id,
                   i.report_count AS incident_report_count,
                   i.title        AS incident_title
            FROM reports r
            LEFT JOIN incidents i ON i.incident_id = r.incident_id
            WHERE r.report_id = %s
            """,
            (report_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


_INCIDENT_COLUMNS = """
    incident_id, title, category, ward_id,
    ST_Y(centroid::geometry) AS lat, ST_X(centroid::geometry) AS lon,
    first_reported_at, last_reported_at, report_count, distinct_reporters,
    priority_score, priority_band, factor_breakdown, why,
    department_id, cc_departments, sla_due_at, status,
    super_incident_id, created_at, updated_at
"""


def list_incidents(
    *,
    ward: str | None = None,
    department: str | None = None,
    band: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Incidents matching the given filters, ranked highest priority first.

    Sorted the same way as `incidents_priority_idx` (PRD section 12 dashboard
    queue): priority_score descending, nulls last so un-prioritized incidents
    sink rather than sorting ambiguously.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if ward is not None:
        clauses.append("ward_id = %(ward)s")
        params["ward"] = ward
    if department is not None:
        clauses.append("department_id = %(department)s")
        params["department"] = department
    if band is not None:
        clauses.append("priority_band = %(band)s")
        params["band"] = band
    if status is not None:
        clauses.append("status = %(status)s")
        params["status"] = status

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_INCIDENT_COLUMNS}
            FROM incidents
            {where}
            ORDER BY priority_score DESC NULLS LAST, first_reported_at
            LIMIT %(limit)s
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def incident(incident_id: str) -> dict[str, Any] | None:
    """One incident's full record, or None."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE incident_id = %s",
            (incident_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def quarantine_depth() -> int:
    """Envelopes awaiting human triage."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM quarantine WHERE status = 'pending'")
        row = cur.fetchone()
        return int(row["n"]) if row else 0


def list_quarantine(
    *, status: str | None = "pending", topic: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Quarantined envelopes for the triage surface (PRD section 8.2, P4).

    Defaults to `pending` — the queue a human actually needs to work through —
    but a reviewer can pass `status=None` to see the full history including
    what has already been released or discarded.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if status is not None:
        clauses.append("status = %(status)s")
        params["status"] = status
    if topic is not None:
        clauses.append("topic = %(topic)s")
        params["topic"] = topic
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT quarantine_id, message_id, trace_id, topic, verdict_id,
                   reasons, status, quarantined_at, reviewed_at, reviewed_by
            FROM quarantine
            {where}
            ORDER BY quarantined_at DESC
            LIMIT %(limit)s
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def mark_quarantine_reviewed(
    quarantine_id: str, *, status: str, reviewed_by: str, note: str | None = None
) -> None:
    """Record a human triage decision on one quarantined envelope.

    `status` is `released` or `discarded` — enforced by the same CHECK
    constraint `db/migrations/0001_init.sql` put on the column, so an invalid
    value fails loudly at the database rather than silently no-opping.
    `note` is not persisted (the `quarantine` table has no column for it,
    see `docs/data-model.md`); it is accepted so a reviewer's reasoning is at
    least visible in the API log, not a promise of a durable audit field.
    """
    del note  # accepted, logged by the caller, not stored - see docstring
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE quarantine
            SET status = %s, reviewed_at = now(), reviewed_by = %s
            WHERE quarantine_id = %s
            """,
            (status, reviewed_by, quarantine_id),
        )


def quarantine_envelope(quarantine_id: str) -> dict[str, Any] | None:
    """One quarantined envelope's full record, envelope included, or None."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT quarantine_id, message_id, trace_id, topic, verdict_id,
                   envelope, reasons, status, quarantined_at, reviewed_at, reviewed_by
            FROM quarantine
            WHERE quarantine_id = %s
            """,
            (quarantine_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None
