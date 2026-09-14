"""A3 — Incident Synthesis, consolidation mode (PRD section 7/A3).

**Job:** "Are all these reports actually one problem?"
**In:** `reports.linked`. **Out:** `incidents.updated`.

Consolidation creates or updates the `Incident` record: canonical title,
canonical location, first-reported time, report count, member reports.

Pattern detection — the windowed ward x 48 h search for co-occurring categories
that implies a shared cause, and the SuperIncidents it produces — is the other
half of A3 and is **not implemented here**. It is P5 (M4). This module is
consolidation only, and says so in every event it emits via `mode`.

## Canonical location

PRD section 7/A3: "weighted centroid of member reports, weighted by GPS
accuracy". A report accurate to 5 m should move the centroid far more than one
accurate to 200 m, so each member contributes weight `1 / max(accuracy, 1)`.
The arithmetic is done in PostGIS so the result is a real geography point
rather than a naive average of degrees.

## Arbitrating the grey zone

A2 sends scores between 0.60 and 0.82 here as `candidate_link` for A3 to
arbitrate "with an LLM adjudication call" (PRD section 7/A2).

**No LLM adjudication happens**, because no provider offers it (PRD open
question 2), and calling a keyword comparison "LLM adjudication" would be the
exact dishonesty this codebase avoids. A deterministic adjudicator stands in.

The substitution is principled rather than convenient. The grey zone exists
because the *semantic* component is uncertain, and a lexical embedding makes it
systematically more uncertain still — paraphrases of one pothole score 0.4-0.7
where a real embedding would score 0.85+. But the other two components do not
depend on a language model at all. Spatial and temporal proximity are measured,
not inferred.

So the adjudicator asks a question the baseline can actually answer: are these
two reports comfortably inside *both* of their category's own bounds? Each
component must clear 0.75, which means within the inner quarter of the
category's radius and window — for a pothole, 19 m and 5 days; for garbage,
37 m and 1.8 days. Reports that close, in the same category, are the same
problem regardless of how differently two people worded it. A small semantic
floor still applies, so two reports that share a location but describe visibly
different things do not merge on geometry alone.

PRD section 15 endorses this ordering directly: "cheap deterministic gates
first; LLM only in the grey zone". The gate here is deterministic and narrow;
the LLM will widen it when one is configured.

Anything the adjudicator declines does **not** merge. That is the safe
direction: PRD section 15 rates "false merges hide distinct problems" as
high-impact, and a duplicate incident is cheaper than a hidden one.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from agents.base import Agent
from common.envelope import Envelope
from common.ids import uuid7
from common.logging import get_logger
from common.matching import AUTO_LINK_THRESHOLD, Candidate, bounds_for, score_candidates

__all__ = [
    "ADJUDICATION_FLOORS",
    "Adjudication",
    "SynthesisAgent",
    "adjudicate",
]

log = get_logger(__name__)

#: Consolidation only. Pattern mode arrives in P5.
MODE = "consolidation"

#: What the deterministic adjudicator demands of a grey-zone candidate.
#:
#: Spatial and temporal are model-independent measurements, so they carry the
#: decision. 0.75 means "inside the inner quarter of this category's own radius
#: and window" — the category bounds already encode how far apart two reports
#: of the same thing can plausibly be, and this asks for comfortably inside
#: them, not merely within them.
#:
#: The semantic floor is low on purpose. It is not evidence *for* a merge; it
#: only blocks one when the text clearly describes something else.
ADJUDICATION_FLOORS: dict[str, float] = {
    "spatial": 0.75,
    "temporal": 0.75,
    "semantic": 0.25,
}


class Adjudication(NamedTuple):
    """The outcome of arbitrating one grey-zone candidate."""

    merge: bool
    method: str
    reason: str


def adjudicate(component_scores: dict[str, Any]) -> Adjudication:
    """Resolve a `candidate_link` (PRD section 7/A2's 0.60-0.82 band).

    Deterministic by necessity — see the module docstring. A future
    LLM-capable provider replaces this, and the `method` field on the result is
    what makes which one ran visible in the trace.
    """
    failed = []
    for component, floor in ADJUDICATION_FLOORS.items():
        value = component_scores.get(component)
        if value is None or float(value) < floor:
            shown = "n/a" if value is None else f"{float(value):.2f}"
            failed.append(f"{component}={shown} < {floor}")

    if failed:
        return Adjudication(
            merge=False,
            method="deterministic",
            reason=f"declined: {'; '.join(failed)}",
        )

    return Adjudication(
        merge=True,
        method="deterministic",
        reason=(
            "spatial and temporal both inside the inner quarter of the "
            "category's radius and window, and the text is not about "
            "something else"
        ),
    )


class SynthesisAgent(Agent):
    """Creates and updates incidents from linked reports."""

    name = "A3"
    version = "1.0.0"
    input_topic = "reports.linked"

    def handle(self, envelope: Envelope) -> list[Envelope]:
        payload = envelope.payload
        report_id = payload["report_id"]
        decision = payload["decision"]

        verdict: Adjudication | None = None
        recheck: Candidate | None = None

        if decision == "auto_link" and payload.get("incident_id"):
            incident_id = str(payload["incident_id"])
            action = "joined"
        elif decision == "candidate_link" and payload.get("incident_id"):
            verdict = adjudicate(payload.get("component_scores") or {})
            if verdict.merge:
                incident_id = str(payload["incident_id"])
                action = "joined after adjudication"
            else:
                incident_id, action, recheck = self._seed_or_join(payload)
        else:
            incident_id, action, recheck = self._seed_or_join(payload)

        self._attach(report_id, incident_id)
        incident = self._recompute(incident_id)

        if incident is None:
            # The only way here is a report row that vanished between A2 and
            # A3. Fail loudly rather than emit an incident with no members.
            raise ValueError(
                f"report {report_id} has no persisted row; cannot consolidate incident"
            )

        log.info(
            "incident consolidated",
            extra={
                "incident_id": incident_id,
                "action": action,
                "report_count": incident["report_count"],
            },
        )

        return [
            envelope.derive(
                topic="incidents.updated",
                producer=self.producer,
                confidence=self._confidence(decision, incident, verdict),
                rationale=self._rationale(action, decision, payload, incident, verdict, recheck),
                payload={
                    "incident_id": incident_id,
                    "mode": MODE,
                    "title": incident["title"],
                    "category": incident["category"],
                    "centroid": {
                        "lat": float(incident["lat"]),
                        "lon": float(incident["lon"]),
                    },
                    "ward_id": incident["ward_id"],
                    "first_reported_at": _iso(incident["first_reported_at"]),
                    "last_reported_at": _iso(incident["last_reported_at"]),
                    "report_count": int(incident["report_count"]),
                    "distinct_reporters": int(incident["distinct_reporters"]),
                    "member_report_ids": [str(rid) for rid in incident["member_report_ids"]],
                    "status": incident["status"],
                    "super_incident": None,
                },
                correlation_id=incident_id,
            )
        ]

    # -- the authoritative re-check ---------------------------------------

    def _seed_or_join(self, payload: dict[str, Any]) -> tuple[str, str, Candidate | None]:
        """Decide between seeding and joining, against *current* state.

        A2 scored this report against the incidents that existed when it
        looked. Agents run concurrently and PRD section 9.4 orders messages
        only per correlation_id, so several reports of one pothole can all be
        scored before any of their incidents exists — and all seed.

        A3 owns `incidents`, so it re-runs the same scoring here, now, and
        joins if a match has appeared in the meantime. Only ever seed -> join:
        a re-check never undoes a link A2 already made.
        """
        if not self.persist:
            return str(uuid7()), "seeded", None

        report_id = str(payload["report_id"])
        category = self._category_of(report_id)
        if category is None:
            return str(uuid7()), "seeded", None

        radius, window = bounds_for(category)
        candidates = score_candidates(report_id, category, radius, window)
        best = max(candidates, key=lambda c: c.score, default=None)

        if best is None:
            return str(uuid7()), "seeded", None

        # Same bar A2 and the adjudicator apply, so a re-check cannot merge
        # anything the ordinary path would have refused.
        if best.score >= AUTO_LINK_THRESHOLD or adjudicate(best.component_scores()).merge:
            log.info(
                "late match found on re-check",
                extra={
                    "report_id": report_id,
                    "incident_id": best.incident_id,
                    "score": best.score,
                },
            )
            return best.incident_id, "joined on re-check", best

        return str(uuid7()), "seeded", best

    def _category_of(self, report_id: str) -> str | None:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT category FROM reports WHERE report_id = %s", (report_id,))
            row = cur.fetchone()
        return str(row["category"]) if row and row["category"] else None

    # -- persistence ------------------------------------------------------

    def _attach(self, report_id: str, incident_id: str) -> None:
        """Point the report at its incident, creating the incident if needed.

        A3 owns `incidents` and the `reports.incident_id` link; it touches no
        other agent's columns.
        """
        if not self.persist:
            return

        from common.db import transaction

        with transaction() as conn, conn.cursor() as cur:
            # Seed a placeholder row so the foreign key holds; _recompute fills
            # every derived field from the members immediately after.
            cur.execute(
                """
                INSERT INTO incidents (
                    incident_id, title, category, centroid, ward_id,
                    first_reported_at, last_reported_at, status
                )
                SELECT %(incident_id)s,
                       coalesce(r.summary, 'Untitled incident'),
                       coalesce(r.category, 'other'),
                       r.geom,
                       r.ward_id,
                       r.created_at,
                       r.created_at,
                       'open'
                FROM reports r
                WHERE r.report_id = %(report_id)s
                ON CONFLICT (incident_id) DO NOTHING
                """,
                {"incident_id": incident_id, "report_id": report_id},
            )
            cur.execute(
                "UPDATE reports SET incident_id = %s, status = 'linked', updated_at = now() "
                "WHERE report_id = %s",
                (incident_id, report_id),
            )

    def _recompute(self, incident_id: str) -> dict[str, Any] | None:
        """Rebuild every derived field from the incident's members.

        Recomputing rather than incrementing means `report_count` can never
        drift from the actual membership — one of the A3 invariants Sentinel
        checks in PRD section 8.1.
        """
        if not self.persist:
            return None

        from common.db import transaction

        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH members AS (
                    SELECT report_id, geom, created_at, device_hash, summary,
                           category, ward_id, severity_raw,
                           -- PRD 7/A3: weight the centroid by GPS accuracy, so a
                           -- 5 m fix counts far more than a 200 m one.
                           1.0 / greatest(coalesce(gps_accuracy_m, 50.0), 1.0) AS weight
                    FROM reports
                    WHERE incident_id = %(incident_id)s
                ),
                aggregate AS (
                    SELECT
                        count(*)                                  AS report_count,
                        count(DISTINCT device_hash)               AS distinct_reporters,
                        min(created_at)                           AS first_reported_at,
                        max(created_at)                           AS last_reported_at,
                        array_agg(report_id ORDER BY created_at)  AS member_report_ids,
                        sum(ST_X(geom::geometry) * weight) / sum(weight) AS lon,
                        sum(ST_Y(geom::geometry) * weight) / sum(weight) AS lat,
                        mode() WITHIN GROUP (ORDER BY category)   AS category,
                        mode() WITHIN GROUP (ORDER BY ward_id)    AS ward_id
                    FROM members
                ),
                title_source AS (
                    -- The most severe member's summary makes the best title:
                    -- it describes the worst the incident has been reported as.
                    SELECT summary FROM members
                    ORDER BY severity_raw DESC NULLS LAST, created_at
                    LIMIT 1
                )
                UPDATE incidents i
                SET report_count       = a.report_count,
                    distinct_reporters = a.distinct_reporters,
                    first_reported_at  = a.first_reported_at,
                    last_reported_at   = a.last_reported_at,
                    centroid           = ST_SetSRID(ST_MakePoint(a.lon, a.lat), 4326)::geography,
                    category           = coalesce(a.category, i.category),
                    ward_id            = coalesce(a.ward_id, i.ward_id),
                    title              = coalesce((SELECT summary FROM title_source), i.title),
                    updated_at         = now()
                FROM aggregate a
                WHERE i.incident_id = %(incident_id)s
                  AND a.report_count > 0
                RETURNING i.incident_id, i.title, i.category, i.ward_id,
                          i.first_reported_at, i.last_reported_at, i.report_count,
                          i.distinct_reporters, i.status,
                          ST_Y(i.centroid::geometry) AS lat,
                          ST_X(i.centroid::geometry) AS lon,
                          a.member_report_ids
                """,
                {"incident_id": incident_id},
            )
            row = cur.fetchone()

        return dict(row) if row else None

    # -- narration --------------------------------------------------------

    @staticmethod
    def _confidence(decision: str, incident: dict[str, Any], verdict: Adjudication | None) -> float:
        """Corroboration is the strongest signal that a consolidation is right."""
        if decision == "auto_link":
            return 0.9
        if verdict is not None:
            # An adjudicated merge is a real decision, but a deterministic one
            # made without the semantic evidence the grey zone was about.
            return 0.7 if verdict.merge else 0.6
        return 0.85 if int(incident["report_count"]) > 1 else 0.8

    @staticmethod
    def _rationale(
        action: str,
        decision: str,
        payload: dict[str, Any],
        incident: dict[str, Any],
        verdict: Adjudication | None,
        recheck: Candidate | None,
    ) -> str:
        base = (
            f"Report {action}; incident now has {incident['report_count']} report(s) "
            f"from {incident['distinct_reporters']} distinct device(s). "
            f"Centroid recomputed weighted by GPS accuracy."
        )
        if recheck is not None and action == "joined on re-check":
            return (
                f"{base} A2 found no match, but an incident matching at "
                f"{recheck.score:.2f} existed by the time this was consolidated - "
                f"concurrent reports of one problem. Joined it rather than seeding "
                f"a duplicate."
            )

        if verdict is None:
            return base

        return (
            f"{base} A2 scored {payload.get('match_score', 0):.2f} against incident "
            f"{payload.get('incident_id')}, inside the 0.60-0.82 grey zone. "
            f"Adjudicated by the {verdict.method} adjudicator (no LLM adjudicator "
            f"is configured): {verdict.reason}."
        )


def _iso(value: Any) -> str:
    """Timestamps out of psycopg are aware datetimes; the schema wants RFC 3339."""
    rendered: str = value.isoformat()
    return rendered.replace("+00:00", "Z")
