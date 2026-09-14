"""Read queries behind the API.

Read-only by construction: nothing in this module writes. The API surface in
PRD section 12 that mutates (report submission, resolution claims) belongs to
the agents that own those tables and arrives with them in P2 and P5.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from common.db import connect

__all__ = ["agent_activity", "trace_messages", "trace_runs", "trace_verdicts", "verdict_mix"]


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


def quarantine_depth() -> int:
    """Envelopes awaiting human triage."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM quarantine WHERE status = 'pending'")
        row = cur.fetchone()
        return int(row["n"]) if row else 0
