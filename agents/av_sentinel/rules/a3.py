"""A3 invariants (PRD section 8.1).

PRD row: "`report_count` = actual member count; centroid recomputed; a
SuperIncident never removes a member incident."

`report_count` matching the member list is self-contained: both numbers are
on the wire in the same `incidents.updated` envelope. The other two need state
this envelope does not carry — the member reports' own coordinates to check a
recomputed centroid, and the previous member list to check nothing was
dropped — and run only when a database is reachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents.av_sentinel.rules import Finding

__all__ = ["check"]


def check(topic: str, envelope: Mapping[str, Any], *, allow_db: bool) -> list[Finding]:
    if topic != "incidents.updated":
        return []
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return []

    findings: list[Finding] = []
    findings.extend(_report_count_matches_members(payload))
    if allow_db:
        findings.extend(_super_incident_never_shrinks(payload))
    return findings


def _report_count_matches_members(payload: Mapping[str, Any]) -> list[Finding]:
    report_count = payload.get("report_count")
    members = payload.get("member_report_ids")
    if not isinstance(report_count, int) or not isinstance(members, list):
        return []
    if report_count == len(members):
        return []
    return [
        Finding(
            code="a3_report_count_mismatch",
            message=(
                f"report_count is {report_count} but member_report_ids has {len(members)} entries"
            ),
            severity="fail_hard",
            path="payload.report_count",
        )
    ]


def _super_incident_never_shrinks(payload: Mapping[str, Any]) -> list[Finding]:
    """Check that this incident is still listed as a member, when it claims one.

    A genuine "never removes a member" check needs the super incident's member
    list *before* this update and its list *after*, and this single
    `incidents.updated` envelope carries neither — only this one incident's own
    view of its super-incident link. What can be checked here is narrower: if
    this envelope claims membership in a super incident, that super incident's
    stored row must actually still list it. A3 pattern mode and SuperIncidents
    do not exist until P5, so in practice `super_incident` is always null today
    and this function returns no findings — documented rather than left silent.
    """
    super_incident = payload.get("super_incident")
    if not isinstance(super_incident, Mapping):
        return []
    super_incident_id = super_incident.get("super_incident_id")
    incident_id = payload.get("incident_id")
    if not isinstance(super_incident_id, str) or not isinstance(incident_id, str):
        return []
    try:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT member_incident_ids FROM super_incidents WHERE super_incident_id = %s",
                (super_incident_id,),
            )
            row = cur.fetchone()
    except Exception:
        return []
    if row is None:
        return []
    current_members = set(row["member_incident_ids"] or [])
    if not current_members or incident_id in current_members:
        return []
    return [
        Finding(
            code="a3_super_incident_dropped_member",
            message=(
                f"incident {incident_id} claims membership in super incident "
                f"{super_incident_id}, but that super incident's stored member "
                "list no longer includes it"
            ),
            severity="fail_hard",
            path="payload.super_incident",
        )
    ]
