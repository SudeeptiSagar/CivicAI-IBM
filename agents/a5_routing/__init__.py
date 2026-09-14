"""A5 — Routing (PRD section 7/A5).

**Job:** "Whose queue does this land in, and by when?"
**In:** `incidents.prioritized`. **Out:** `incidents.routed` or `incidents.unrouted`.

Deterministic policy over two small tables: a category -> department map (this
module's business logic, kept as a Python constant rather than reference data
because it encodes a routing *decision*, not a fact about the world) and the
`departments` registry seeded by migration `0001` (an actual fact: which
departments exist and what their default SLA is).

## Never a silent default

PRD section 7/A5 and the `incidents.unrouted` schema are explicit: a category
this module cannot map, or a mapped department that (defensively) turns out not
to be in the registry, goes to `incidents.unrouted` with a reason code. It never
falls back to an arbitrary department. `"other"` is deliberately **not** in
`CATEGORY_DEPARTMENT` — it is the taxonomy's catch-all for "we don't know what
this is", and routing it anywhere would be inventing an answer this module does
not have.

## The category -> department map, and why each choice was made

    pothole, road_damage, encroachment,
    traffic_obstruction              -> roads_and_infrastructure (road surface
                                         and right-of-way problems)
    waterlogging, drain_overflow,
    water_supply                     -> drainage_and_water
    sewage                           -> health (a public-health hazard first;
                                         there is no separate sanitation
                                         department in the PRD's six)
    garbage, public_toilet           -> solid_waste_management (public toilets
                                         are a sanitation-upkeep problem closer
                                         to waste management's daily rounds
                                         than to health's clinical remit — a
                                         judgement call, not a fact)
    streetlight                      -> electrical_streetlights
    stray_animals                    -> health
    tree_hazard                      -> parks
    other                            -> unmapped by design; -> unrouted

## Ambiguous ownership

PRD section 7/A5 asks for `primary_department` plus `cc_departments[]` for
incidents that cross departmental lines. One rule, kept simple and
deterministic: `waterlogging` and `drain_overflow` are drainage's job to fix,
but standing water on a road makes the road impassable, so
`roads_and_infrastructure` is cc'd. No other category is treated as
cross-cutting here — adding more rules without a concrete incident motivating
them would be over-engineering a v1.

## SLA clock

PRD section 7/A5 wants SLA "by (department, priority band)"; `departments`
only carries one `default_sla_hours` per department. This module layers a
band multiplier on top: a critical incident should get a much shorter window
than the department's default, a low-priority one a longer one.

    critical  x 0.25   quarter the default window
    high      x 0.50   half
    medium    x 1.00   the department's default, unchanged
    low       x 1.50   half again as long

The clock starts at the incident's `first_reported_at` (when the problem was
first known about, not when routing happened to run), falling back to now()
only if that is unavailable.

## Open gap: no office-level jurisdiction

PRD asks for a "ward -> jurisdiction lookup". `ward_id` already carries that:
every incident's ward is known from A3, and `wards` (migration `0002`) is the
real jurisdiction table. A separate ward -> office table does not exist yet —
there is no office/beat-level dataset for this build — so `office_id` on
`incidents.routed` is always `null`. That is an honest, recorded gap, not an
invented office id.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from agents.base import Agent, SkipSignal
from common.envelope import Envelope
from common.logging import get_logger

__all__ = [
    "CATEGORY_DEPARTMENT",
    "CROSS_CUTTING_CC",
    "SLA_BAND_MULTIPLIER",
    "RoutingAgent",
    "cc_departments_for",
    "department_for_category",
    "sla_due_at_for",
    "sla_hours_for",
]

log = get_logger(__name__)

#: Category -> department. Absence is deliberate for "other" (see module
#: docstring): it must reach `incidents.unrouted`, never a guessed default.
CATEGORY_DEPARTMENT: Final[dict[str, str]] = {
    "pothole": "roads_and_infrastructure",
    "road_damage": "roads_and_infrastructure",
    "encroachment": "roads_and_infrastructure",
    "traffic_obstruction": "roads_and_infrastructure",
    "waterlogging": "drainage_and_water",
    "drain_overflow": "drainage_and_water",
    "water_supply": "drainage_and_water",
    "sewage": "health",
    "garbage": "solid_waste_management",
    "public_toilet": "solid_waste_management",
    "streetlight": "electrical_streetlights",
    "stray_animals": "health",
    "tree_hazard": "parks",
}

#: The one ambiguous-ownership rule this module implements. See module
#: docstring: standing water is drainage's fix but a road-usability problem.
CROSS_CUTTING_CC: Final[dict[str, tuple[str, ...]]] = {
    "waterlogging": ("roads_and_infrastructure",),
    "drain_overflow": ("roads_and_infrastructure",),
}

#: SLA hours = department.default_sla_hours * this multiplier (see docstring).
SLA_BAND_MULTIPLIER: Final[dict[str, float]] = {
    "critical": 0.25,
    "high": 0.50,
    "medium": 1.00,
    "low": 1.50,
}

#: Fallback default SLA hours, used only when routing is exercised with no
#: database (`persist=False`, e.g. tests exercising `handle()` directly rather
#: than the pure functions below). Mirrors migration `0001`'s seed values
#: exactly, so this constant must be kept in sync with that seed by hand.
_FALLBACK_DEFAULT_SLA_HOURS: Final[dict[str, float]] = {
    "roads_and_infrastructure": 72,
    "drainage_and_water": 48,
    "solid_waste_management": 24,
    "electrical_streetlights": 48,
    "health": 24,
    "parks": 120,
}


def department_for_category(category: str | None) -> str | None:
    """The mapped department, or None if `category` is unmapped/unknown."""
    if category is None:
        return None
    return CATEGORY_DEPARTMENT.get(category)


def cc_departments_for(category: str, primary: str) -> list[str]:
    """Departments to cc, from the one ambiguous-ownership rule above."""
    return [dept for dept in CROSS_CUTTING_CC.get(category, ()) if dept != primary]


def sla_hours_for(default_sla_hours: float, priority_band: str) -> float:
    """Department default scaled by the priority band's multiplier."""
    multiplier = SLA_BAND_MULTIPLIER.get(priority_band, 1.0)
    return round(default_sla_hours * multiplier, 4)


def sla_due_at_for(base_time: dt.datetime, sla_hours: float) -> dt.datetime:
    """The SLA deadline: `base_time` (usually first_reported_at) + sla_hours."""
    return base_time + dt.timedelta(hours=sla_hours)


class RoutingAgent(Agent):
    """Routes prioritized incidents to a department, or to human triage."""

    name = "A5"
    version = "1.0.0"
    input_topic = "incidents.prioritized"

    def handle(self, envelope: Envelope) -> list[Envelope]:
        payload = envelope.payload
        incident_id = str(payload["incident_id"])
        priority_band = str(payload["priority_band"])

        if self.persist:
            incident = self._incident(incident_id)
            if incident is None:
                # The only way here is an incident row that vanished between
                # A3/A4 and A5 -- fail loudly rather than route nothing.
                raise SkipSignal(
                    "incident_vanished",
                    f"incident {incident_id} has no incident row; nothing to route",
                )
            category = incident["category"]
            ward_id = incident["ward_id"]
            first_reported_at = incident["first_reported_at"]
        else:
            category, ward_id, first_reported_at = None, None, None

        primary = department_for_category(category)
        if primary is None:
            reason = f"Category {category!r} has no department mapping; needs human triage."
            return [self._unrouted(envelope, incident_id, category, "unknown_category", reason)]

        default_sla_hours = self._default_sla_hours(primary)
        if default_sla_hours is None:
            return [
                self._unrouted(
                    envelope,
                    incident_id,
                    category,
                    "no_jurisdiction_match",
                    f"Mapped department {primary!r} is not in the departments registry.",
                )
            ]

        cc = cc_departments_for(category, primary)
        sla_hours = sla_hours_for(default_sla_hours, priority_band)
        base_time = first_reported_at or _utcnow()
        sla_due_at = sla_due_at_for(base_time, sla_hours)

        if self.persist:
            self._persist(incident_id, primary, cc, sla_due_at)

        rationale = self._rationale(category, primary, cc, priority_band, sla_hours)

        log.info(
            "incident routed",
            extra={
                "incident_id": incident_id,
                "primary_department": primary,
                "cc_departments": cc,
                "sla_hours": sla_hours,
            },
        )

        return [
            envelope.derive(
                topic="incidents.routed",
                producer=self.producer,
                confidence=0.95 if not cc else 0.85,
                rationale=rationale,
                payload={
                    "incident_id": incident_id,
                    "primary_department": primary,
                    "cc_departments": cc,
                    "ward_id": ward_id,
                    "office_id": None,  # no ward->office table exists yet; honest gap
                    "sla_due_at": _iso(sla_due_at),
                    "sla_hours": sla_hours,
                    "priority_band": priority_band,
                },
            )
        ]

    # -- reads and persistence (A5 owns department_id/cc_departments/sla_due_at/status) --

    def _incident(self, incident_id: str) -> dict[str, Any] | None:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT category, ward_id, first_reported_at FROM incidents WHERE incident_id = %s",
                (incident_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def _default_sla_hours(self, department_id: str) -> float | None:
        if not self.persist:
            return _FALLBACK_DEFAULT_SLA_HOURS.get(department_id)

        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT default_sla_hours FROM departments WHERE department_id = %s",
                (department_id,),
            )
            row = cur.fetchone()
        return float(row["default_sla_hours"]) if row else None

    def _persist(
        self, incident_id: str, primary: str, cc: list[str], sla_due_at: dt.datetime
    ) -> None:
        from common.db import transaction

        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE incidents
                SET department_id  = %(department_id)s,
                    cc_departments = %(cc)s,
                    sla_due_at     = %(sla_due_at)s,
                    status         = 'routed',
                    updated_at     = now()
                WHERE incident_id = %(incident_id)s
                """,
                {
                    "department_id": primary,
                    "cc": cc,
                    "sla_due_at": sla_due_at,
                    "incident_id": incident_id,
                },
            )

    # -- unrouted -----------------------------------------------------------

    def _unrouted(
        self,
        envelope: Envelope,
        incident_id: str,
        attempted_category: str | None,
        reason_code: str,
        reason: str,
    ) -> Envelope:
        log.info(
            "incident unrouted",
            extra={"incident_id": incident_id, "reason_code": reason_code},
        )
        return envelope.derive(
            topic="incidents.unrouted",
            producer=self.producer,
            confidence=0.9,
            rationale=reason,
            payload={
                "incident_id": incident_id,
                "attempted_category": attempted_category,
                "reason_code": reason_code,
                "reason": reason,
            },
        )

    def output_topic_for_skip(self, envelope: Envelope) -> str:
        # An A5 skip (e.g. incident vanished) stands in for "no routing
        # decision at all", which reads more honestly against the input topic
        # than against either possible output topic.
        return self.input_topic

    # -- narration ------------------------------------------------------

    @staticmethod
    def _rationale(
        category: str, primary: str, cc: list[str], priority_band: str, sla_hours: float
    ) -> str:
        base = (
            f"Category {category!r} maps to {primary}; SLA {sla_hours:.1f}h "
            f"({priority_band} band multiplier applied to the department default)."
        )
        if cc:
            base += f" Cc'd to {', '.join(cc)}: standing water affects road usability too."
        return base[:2000]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
