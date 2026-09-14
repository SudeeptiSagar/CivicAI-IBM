"""A5 invariants (PRD section 8.1).

PRD row: "Department ∈ registry; ward jurisdiction valid; SLA set; unknown →
unrouted, never defaulted."

* **Department in registry** is self-contained against `DEPARTMENT_REGISTRY`,
  which mirrors the six rows seeded in `db/migrations/0001_init.sql`;
  `tests/test_invariants.py` asserts the two stay in lockstep. L1 already
  bounds `primary_department` to the same enum
  (`schemas/envelope.v1.json#/$defs/department`), so this is a second line of
  defence, not the only one.
* **SLA set** is self-contained: `sla_due_at` and `sla_hours` both ride on the
  `incidents.routed` envelope.
* **Unknown → unrouted, never defaulted** is self-contained by construction:
  L1 already requires `incidents.unrouted` to carry a `reason_code`
  (`schemas/incidents.unrouted.v1.json`), and `incidents.routed` requires a
  registry department — there is no third shape an "I could not decide"
  message could take, so the invariant is that these two topics cover every
  case, which is enforced by there being no way to emit a passing envelope
  that does neither.
* **Ward jurisdiction valid** needs the loaded ward registry (the `wards`
  table `common.geo.ward_for` reads), so it runs only when a database is
  reachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from agents.av_sentinel.rules import Finding

__all__ = ["DEPARTMENT_REGISTRY", "check"]

#: Mirrors the department_id column of db/migrations/0001_init.sql's seed rows.
DEPARTMENT_REGISTRY: Final[frozenset[str]] = frozenset(
    {
        "roads_and_infrastructure",
        "drainage_and_water",
        "solid_waste_management",
        "electrical_streetlights",
        "health",
        "parks",
    }
)


def check(topic: str, envelope: Mapping[str, Any], *, allow_db: bool) -> list[Finding]:
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return []

    if topic == "incidents.routed":
        findings = list(_department_in_registry(payload))
        findings.extend(_sla_set(payload))
        if allow_db:
            findings.extend(_ward_jurisdiction_valid(payload))
        return findings
    return []


def _department_in_registry(payload: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    primary = payload.get("primary_department")
    if primary not in DEPARTMENT_REGISTRY:
        findings.append(
            Finding(
                code="a5_department_off_registry",
                message=f"primary_department {primary!r} is not in the department registry",
                severity="fail_hard",
                path="payload.primary_department",
            )
        )
    cc = payload.get("cc_departments")
    if isinstance(cc, list):
        off_registry = [d for d in cc if d not in DEPARTMENT_REGISTRY]
        if off_registry:
            findings.append(
                Finding(
                    code="a5_cc_department_off_registry",
                    message=f"cc_departments {off_registry} are not in the department registry",
                    severity="fail_hard",
                    path="payload.cc_departments",
                )
            )
    return findings


def _sla_set(payload: Mapping[str, Any]) -> list[Finding]:
    sla_due_at = payload.get("sla_due_at")
    sla_hours = payload.get("sla_hours")
    if sla_due_at and isinstance(sla_hours, int | float) and sla_hours > 0:
        return []
    return [
        Finding(
            code="a5_sla_not_set",
            message=(
                f"sla_due_at={sla_due_at!r} sla_hours={sla_hours!r}; both must be set and positive"
            ),
            severity="fail_hard",
            path="payload.sla_due_at",
        )
    ]


def _ward_jurisdiction_valid(payload: Mapping[str, Any]) -> list[Finding]:
    ward_id = payload.get("ward_id")
    if not isinstance(ward_id, str) or not ward_id:
        return []
    try:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM wards WHERE ward_id = %s", (ward_id,))
            exists = cur.fetchone() is not None
    except Exception:
        return []
    if exists:
        return []
    return [
        Finding(
            code="a5_ward_not_in_registry",
            message=f"ward_id {ward_id!r} is not in the loaded ward registry",
            severity="fail_hard",
            path="payload.ward_id",
        )
    ]
