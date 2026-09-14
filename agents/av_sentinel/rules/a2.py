"""A2 invariants (PRD section 8.1).

PRD row: "An incident's members share a category; every member within max
radius of centroid; no report in two incidents; merge count monotonic."

All four named invariants are about the state of the `incidents` table *after*
A3 has consolidated — they are not decidable from one `reports.linked` envelope
in isolation, which carries a proposal, not incident membership. They run only
when a database is reachable (`allow_db=True`), reading the same tables A3
writes, and are silently skipped otherwise (see `rules/__init__.py`).

One additional invariant is self-contained and included because the PRD names
the thresholds it protects in the same section (7/A2): the `decision` field
must actually agree with `match_score` against `common.matching`'s own
published thresholds. A2 emitting `auto_link` at 0.55, say, would be exactly
the kind of quiet drift Sentinel exists to catch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents.av_sentinel.rules import Finding
from common.matching import AUTO_LINK_THRESHOLD, CANDIDATE_THRESHOLD

__all__ = ["check"]


def check(topic: str, envelope: Mapping[str, Any], *, allow_db: bool) -> list[Finding]:
    if topic != "reports.linked":
        return []
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return []

    findings: list[Finding] = []
    findings.extend(_decision_matches_score(payload))
    if allow_db:
        findings.extend(_no_report_in_two_incidents(payload))
        findings.extend(_incident_members_share_category(payload))
    return findings


def _decision_matches_score(payload: Mapping[str, Any]) -> list[Finding]:
    decision = payload.get("decision")
    score = payload.get("match_score")
    if not isinstance(score, int | float):
        return []

    expected = _expected_decision(float(score))
    if expected is None or decision == expected:
        return []
    return [
        Finding(
            code="a2_decision_score_mismatch",
            message=(
                f"decision {decision!r} does not match match_score {score} "
                f"(auto_link >= {AUTO_LINK_THRESHOLD}, "
                f"candidate_link >= {CANDIDATE_THRESHOLD}, else new_incident_seed)"
            ),
            severity="fail_hard",
            path="payload.decision",
        )
    ]


def _expected_decision(score: float) -> str | None:
    if score >= AUTO_LINK_THRESHOLD:
        return "auto_link"
    if score >= CANDIDATE_THRESHOLD:
        return "candidate_link"
    return "new_incident_seed"


def _no_report_in_two_incidents(payload: Mapping[str, Any]) -> list[Finding]:
    report_id = payload.get("report_id")
    incident_id = payload.get("incident_id")
    if not isinstance(report_id, str) or not isinstance(incident_id, str):
        return []
    try:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT incident_id FROM reports WHERE report_id = %s AND incident_id IS NOT NULL",
                (report_id,),
            )
            row = cur.fetchone()
    except Exception:
        return []
    if row is None or str(row["incident_id"]) in (incident_id, ""):
        return []
    return [
        Finding(
            code="a2_report_already_linked_elsewhere",
            message=(
                f"report {report_id} is already linked to incident "
                f"{row['incident_id']}, cannot also link to {incident_id}"
            ),
            severity="fail_hard",
            path="payload.incident_id",
        )
    ]


def _incident_members_share_category(payload: Mapping[str, Any]) -> list[Finding]:
    incident_id = payload.get("incident_id")
    report_id = payload.get("report_id")
    if not isinstance(incident_id, str) or not isinstance(report_id, str):
        return []
    try:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT category FROM incidents WHERE incident_id = %s", (incident_id,))
            incident_row = cur.fetchone()
            cur.execute("SELECT category FROM reports WHERE report_id = %s", (report_id,))
            report_row = cur.fetchone()
    except Exception:
        return []
    if not incident_row or not report_row:
        return []
    if incident_row["category"] == report_row["category"]:
        return []
    return [
        Finding(
            code="a2_cross_category_link",
            message=(
                f"report {report_id} category {report_row['category']!r} does not match "
                f"incident {incident_id} category {incident_row['category']!r}"
            ),
            severity="fail_hard",
            path="payload.incident_id",
        )
    ]
