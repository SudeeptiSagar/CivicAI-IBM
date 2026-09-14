"""A4 — Prioritization (PRD section 7/A4).

**Job:** "How urgent is this, and why?"
**In:** `incidents.updated`. **Out:** `incidents.prioritized`.

A deterministic six-factor weighted score in [0, 100], never a model call: this
phase is arithmetic over what has already been observed, not reasoning about
text, so the missing watsonx credentials (PRD open question 2) do not block it.

    hazard_severity            0.30   how bad the thing itself is
    exposure                   0.20   how much footfall/traffic sees it
    vulnerable_site_proximity  0.15   distance to the nearest school/hospital
    corroboration               0.15   how many distinct people reported it
    age_unresolved              0.10   how long it has sat open
    velocity                    0.10   is reporting accelerating

Every score is emitted with its full `factor_breakdown` — PRD section 8.1 and a
database CHECK constraint both reject a score with no breakdown, so this agent
never has the option of hiding how a number was reached.

## The pure arithmetic lives here, not in a shared module

Unlike `common/matching.py`, A4's scoring is not shared with another agent —
nothing downstream re-runs it — so it stays in this package rather than in
`common/`. It is still factored as free functions taking primitive inputs
(hazard flags, a road class, a distance, a report count, an age, a velocity
pair) precisely so it can be unit-tested without touching Postgres, the same
discipline `common/matching.py` follows for a different reason.

## The life-safety floor

PRD section 7/A4: a life-safety hazard (open manhole, live wire, collapsed
structure, gas leak — the subset `schemas/envelope.v1.json` calls out
explicitly) floors the score at 85 "regardless of report count". The floor is
applied *after* the weighted sum, not instead of it: `factor_breakdown` still
shows the real, honestly-computed factors, and `life_safety_floor_applied`
records that the floor, not the arithmetic, decided the final number.

## Corroboration is log-scaled and device-deduplicated

`incidents.updated` already carries `distinct_reporters` (A3 counts it by
`device_hash`, never by report count), so this factor gets device dedup for
free. It is then log-scaled rather than linear: the difference between 1 and 5
distinct reporters is a much stronger signal than the difference between 20 and
24, and a linear scale would let a single street's worth of reports saturate
the factor before a diverse, city-wide corroboration signal ever could. The
saturation constant (20) is a judgement call: past roughly 20 independent
reporters, more corroboration is not telling A4 anything new about whether the
problem is real.

## Reference data this factor needs, and its honest limits

`exposure` and `vulnerable_site_proximity` need something to measure against
that PRD section 10's core tables do not carry. Migration `0003` adds two small
tables:

* `poi` — schools and hospitals. `vulnerable_site_proximity` is a genuine
  PostGIS distance computation against these points. The points themselves are
  a small, synthetic, hand-placed fixture (`data/reference/bengaluru_poi.geojson`)
  — **not** a real BBMP/OSM extract. Calling this factor a "model" would be
  dishonest; it is a lookup and a distance computation, nothing more.
* `ward_road_class` — a single dominant road class per ward. This is a coarse
  ward-level proxy standing in for a real road-network join (which would
  classify the incident's actual street, not its whole ward). Building and
  seeding a real road network was judged out of scope for this phase; the
  simplification is documented here and in the migration rather than hidden.

A ward with no `ward_road_class` row, or an incident whose centroid has no
`poi` row within any distance, is not guessed at — `exposure` falls back to the
"unknown" weight (0.3, the same as `local`) and `vulnerable_site_proximity`
falls back to 0.0, both spelled out in the functions below rather than left to
a silent default.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Final

from agents.base import Agent, SkipSignal
from common.envelope import Envelope
from common.logging import get_logger

__all__ = [
    "AGE_SATURATION_DAYS",
    "BAND_CUTOFFS",
    "CORROBORATION_SATURATION",
    "LIFE_SAFETY_FLOOR",
    "LIFE_SAFETY_HAZARDS",
    "ROAD_CLASS_WEIGHT",
    "VELOCITY_SURGE_RATIO",
    "WEIGHTS",
    "PriorityAgent",
    "age_value",
    "apply_life_safety_floor",
    "band_for",
    "compute_factor_breakdown",
    "corroboration_value",
    "exposure_value",
    "hazard_severity_value",
    "proximity_value",
    "velocity_value",
    "weighted_score",
]

log = get_logger(__name__)

#: PRD section 7/A4's six factors. Sums to 1.0, asserted by a unit test and (in
#: P4) by a Sentinel invariant; this dict is the single source of truth both
#: check against.
WEIGHTS: Final[dict[str, float]] = {
    "hazard_severity": 0.30,
    "exposure": 0.20,
    "vulnerable_site_proximity": 0.15,
    "corroboration": 0.15,
    "age_unresolved": 0.10,
    "velocity": 0.10,
}

#: The life-safety subset from `schemas/envelope.v1.json`'s hazard_flag enum
#: comment. Presence of any one of these floors priority_score at 85.
LIFE_SAFETY_HAZARDS: Final[frozenset[str]] = frozenset(
    {"open_manhole", "live_wire", "collapsed_structure", "gas_leak"}
)
LIFE_SAFETY_FLOOR: Final = 85.0

#: Band cutoffs. Chosen so LIFE_SAFETY_FLOOR (85) lands exactly on the
#: critical/high boundary: a life-safety incident is always at least critical,
#: never merely "high", and a very high weighted score without a hazard flag
#: reaches the same band on its own merits.
BAND_CUTOFFS: Final[tuple[tuple[float, str], ...]] = (
    (85.0, "critical"),
    (60.0, "high"),
    (35.0, "medium"),
)

#: log(1+n) saturates slowly by design (see module docstring); past roughly 20
#: distinct reporters more corroboration stops being informative.
CORROBORATION_SATURATION: Final = 20

#: Days of being open before age_unresolved factor reaches its maximum. Two
#: weeks: PRD examples talk about incidents open for days, not months, and by
#: two weeks an unresolved incident's age is already telling the full story an
#: unbounded scale would only dilute.
AGE_SATURATION_DAYS: Final = 14.0

#: recent_24h / trailing_daily_mean at or above this ratio maxes out velocity.
#: 3x the historical daily rate is a defensible "something changed" threshold.
VELOCITY_SURGE_RATIO: Final = 3.0

#: Exposure proxy weights per PRD-undefined road class. "unknown" (no
#: ward_road_class row) is treated the same as "local" -- the least exposed
#: named class -- rather than guessed higher, per the "never invent what you
#: could not read" rule.
ROAD_CLASS_WEIGHT: Final[dict[str, float]] = {
    "arterial": 1.0,
    "collector": 0.6,
    "local": 0.3,
}


# -- pure scoring ----------------------------------------------------------


def hazard_severity_value(hazard_flags: list[str], max_severity_raw: int | None) -> float:
    """How bad the incident itself is, independent of who reported it.

    A life-safety flag (open_manhole, live_wire, collapsed_structure, gas_leak)
    maxes this factor out on its own -- the life-safety floor on the final
    score depends on that flag anyway, so the breakdown should already show
    why. Otherwise this is `severity_raw` (A1's 1-5 scale) normalised to
    [0,1], nudged up slightly if any non-life-safety hazard flag is present
    (a report with *no* hazard flags but severity 5 is treated as less certain
    than one where a specific hazard was actually named).
    """
    if any(flag in LIFE_SAFETY_HAZARDS for flag in hazard_flags):
        return 1.0

    base = (max_severity_raw - 1) / 4.0 if max_severity_raw is not None else 0.0
    if hazard_flags:
        base = min(1.0, base + 0.15)
    return round(max(0.0, min(1.0, base)), 6)


def exposure_value(road_class: str | None) -> float:
    """Coarse ward-level proxy for how much traffic/footfall sees this spot.

    See the module docstring for why this is ward-level rather than a real
    road-segment join.
    """
    return ROAD_CLASS_WEIGHT.get(road_class or "", 0.3)


def proximity_value(distance_m: float | None, max_radius_m: float = 500.0) -> float:
    """1.0 at the POI itself, linearly falling to 0 at `max_radius_m`.

    `distance_m=None` means no `poi` row exists to measure against at all
    (empty reference table, or a lookup that genuinely found nothing) -- that
    is scored as *not* proximate rather than guessed at.
    """
    if distance_m is None or max_radius_m <= 0:
        return 0.0
    return round(max(0.0, min(1.0, 1.0 - distance_m / max_radius_m)), 6)


def corroboration_value(
    distinct_reporters: int, saturation: int = CORROBORATION_SATURATION
) -> float:
    """Log-scaled, device-deduplicated corroboration. See module docstring."""
    if distinct_reporters <= 0:
        return 0.0
    return round(min(1.0, math.log(1 + distinct_reporters) / math.log(1 + saturation)), 6)


def age_value(age_days: float, saturation_days: float = AGE_SATURATION_DAYS) -> float:
    """How long the incident has sat open, capped at `saturation_days`."""
    if age_days <= 0 or saturation_days <= 0:
        return 0.0
    return round(min(1.0, age_days / saturation_days), 6)


def velocity_value(
    recent_24h: int, trailing_mean_per_day: float, surge_ratio: float = VELOCITY_SURGE_RATIO
) -> float:
    """Is reporting accelerating? recent 24h count against the trailing daily mean.

    When there is no trailing history yet (the incident is younger than the
    window it would be compared against), a ratio is meaningless -- this falls
    back to an absolute scale instead of dividing by zero: 5 or more reports
    with no prior baseline is treated as a flare-up in its own right. That
    fallback is a judgement call, documented rather than hidden.
    """
    if recent_24h <= 0:
        return 0.0
    if trailing_mean_per_day > 0:
        ratio = recent_24h / trailing_mean_per_day
        return round(min(1.0, ratio / surge_ratio), 6)
    return round(min(1.0, recent_24h / 5.0), 6)


def compute_factor_breakdown(
    *,
    hazard_flags: list[str],
    max_severity_raw: int | None,
    road_class: str | None,
    distance_to_poi_m: float | None,
    distinct_reporters: int,
    age_days: float,
    recent_24h: int,
    trailing_mean_per_day: float,
) -> list[dict[str, Any]]:
    """The six factors, in PRD order, each `{factor, weight, value, contribution}`."""
    values: dict[str, float] = {
        "hazard_severity": hazard_severity_value(hazard_flags, max_severity_raw),
        "exposure": exposure_value(road_class),
        "vulnerable_site_proximity": proximity_value(distance_to_poi_m),
        "corroboration": corroboration_value(distinct_reporters),
        "age_unresolved": age_value(age_days),
        "velocity": velocity_value(recent_24h, trailing_mean_per_day),
    }
    return [
        {
            "factor": factor,
            "weight": WEIGHTS[factor],
            "value": value,
            "contribution": round(WEIGHTS[factor] * value * 100, 4),
        }
        for factor, value in values.items()
    ]


def weighted_score(factor_breakdown: list[dict[str, Any]]) -> float:
    """Sum of contributions, clamped to [0, 100]."""
    total = sum(float(f["contribution"]) for f in factor_breakdown)
    return round(max(0.0, min(100.0, total)), 4)


def apply_life_safety_floor(score: float, hazard_flags: list[str]) -> tuple[float, bool]:
    """`max(score, 85)` when a life-safety hazard flag is present."""
    if any(flag in LIFE_SAFETY_HAZARDS for flag in hazard_flags):
        return max(score, LIFE_SAFETY_FLOOR), True
    return score, False


def band_for(score: float) -> str:
    """Deterministic band from `priority_score`.

    Cutoffs chosen so 85 (the life-safety floor) is always at least
    "critical": see `BAND_CUTOFFS`'s comment.
    """
    for threshold, band in BAND_CUTOFFS:
        if score >= threshold:
            return band
    return "low"


class PriorityAgent(Agent):
    """Scores incidents from `incidents.updated` and emits `incidents.prioritized`."""

    name = "A4"
    version = "1.0.0"
    input_topic = "incidents.updated"

    def handle(self, envelope: Envelope) -> list[Envelope]:
        payload = envelope.payload
        incident_id = str(payload["incident_id"])
        distinct_reporters = int(payload["distinct_reporters"])
        ward_id = payload.get("ward_id")
        centroid = payload["centroid"]
        age_days = _age_days(str(payload["first_reported_at"]))

        if self.persist:
            hazard_flags, max_severity = self._member_signals(incident_id)
            if hazard_flags is None:
                # The only way here is an incident row that vanished between
                # A3 and A4 -- fail loudly rather than score nothing.
                raise SkipSignal(
                    "incident_vanished",
                    f"incident {incident_id} has no member reports; nothing to prioritize",
                )
            recent_24h, trailing_mean = self._velocity_counts(incident_id)
            road_class = self._road_class(ward_id)
            distance_m = self._nearest_poi_distance_m(centroid)
        else:
            hazard_flags, max_severity = [], None
            recent_24h, trailing_mean = 0, 0.0
            road_class, distance_m = None, None

        factor_breakdown = compute_factor_breakdown(
            hazard_flags=hazard_flags,
            max_severity_raw=max_severity,
            road_class=road_class,
            distance_to_poi_m=distance_m,
            distinct_reporters=distinct_reporters,
            age_days=age_days,
            recent_24h=recent_24h,
            trailing_mean_per_day=trailing_mean,
        )
        weighted = weighted_score(factor_breakdown)
        score, floor_applied = apply_life_safety_floor(weighted, hazard_flags)
        band = band_for(score)
        why = self._why(factor_breakdown, score, floor_applied, band)

        if self.persist:
            self._persist(incident_id, score, band, factor_breakdown, why)

        log.info(
            "incident prioritized",
            extra={
                "incident_id": incident_id,
                "priority_score": score,
                "priority_band": band,
                "life_safety_floor_applied": floor_applied,
            },
        )

        return [
            envelope.derive(
                topic="incidents.prioritized",
                producer=self.producer,
                confidence=self._confidence(floor_applied, road_class, distance_m),
                rationale=why,
                payload={
                    "incident_id": incident_id,
                    "priority_score": score,
                    "priority_band": band,
                    "factor_breakdown": factor_breakdown,
                    "why": why,
                    "life_safety_floor_applied": floor_applied,
                },
            )
        ]

    # -- persistence and reads (A4 owns priority_score/band/factor_breakdown/why) --

    def _member_signals(self, incident_id: str) -> tuple[list[str] | None, int | None]:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT hazard_flags, severity_raw FROM reports WHERE incident_id = %s",
                (incident_id,),
            )
            rows = cur.fetchall()

        if not rows:
            return None, None

        hazard_flags: list[str] = sorted(
            {flag for row in rows for flag in (row["hazard_flags"] or [])}
        )
        severities = [row["severity_raw"] for row in rows if row["severity_raw"] is not None]
        max_severity = max(severities) if severities else None
        return hazard_flags, max_severity

    def _velocity_counts(self, incident_id: str) -> tuple[int, float]:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE created_at >= now() - interval '24 hours')
                        AS recent_24h,
                    count(*) FILTER (WHERE created_at <  now() - interval '24 hours')
                        AS older,
                    min(created_at) FILTER (WHERE created_at < now() - interval '24 hours')
                        AS older_since
                FROM reports
                WHERE incident_id = %s
                """,
                (incident_id,),
            )
            row = cur.fetchone()

        if row is None:
            return 0, 0.0

        recent_24h = int(row["recent_24h"] or 0)
        older = int(row["older"] or 0)
        older_since = row["older_since"]
        if older <= 0 or older_since is None:
            return recent_24h, 0.0

        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
        span_days = max((cutoff - older_since).total_seconds() / 86400.0, 1.0)
        return recent_24h, older / span_days

    def _road_class(self, ward_id: str | None) -> str | None:
        if ward_id is None:
            return None
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT dominant_road_class FROM ward_road_class WHERE ward_id = %s",
                (ward_id,),
            )
            row = cur.fetchone()
        return str(row["dominant_road_class"]) if row else None

    def _nearest_poi_distance_m(self, centroid: dict[str, Any]) -> float | None:
        from common.db import connect

        point_wkt = f"POINT({centroid['lon']} {centroid['lat']})"
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT min(ST_Distance(geom, ST_GeogFromText(%s))) AS d FROM poi",
                (point_wkt,),
            )
            row = cur.fetchone()
        distance = row["d"] if row else None
        return float(distance) if distance is not None else None

    def _persist(
        self,
        incident_id: str,
        score: float,
        band: str,
        factor_breakdown: list[dict[str, Any]],
        why: str,
    ) -> None:
        from common.db import Json, transaction

        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE incidents
                SET priority_score   = %(score)s,
                    priority_band    = %(band)s,
                    factor_breakdown = %(factor_breakdown)s,
                    why              = %(why)s,
                    updated_at       = now()
                WHERE incident_id = %(incident_id)s
                """,
                {
                    "score": score,
                    "band": band,
                    "factor_breakdown": Json(factor_breakdown).dumps(),
                    "why": why,
                    "incident_id": incident_id,
                },
            )

    # -- narration --------------------------------------------------------

    @staticmethod
    def _confidence(floor_applied: bool, road_class: str | None, distance_m: float | None) -> float:
        """A life-safety floor is an unambiguous call; missing reference data
        (no road class, no POI in range) means two of the six factors were
        scored on a documented fallback rather than a real measurement."""
        if floor_applied:
            return 0.9
        degraded = road_class is None or distance_m is None
        return 0.65 if degraded else 0.8

    @staticmethod
    def _why(
        factor_breakdown: list[dict[str, Any]], score: float, floor_applied: bool, band: str
    ) -> str:
        dominant = max(factor_breakdown, key=lambda f: float(f["contribution"]))
        base = (
            f"Priority {score:.0f} ({band}), driven mainly by "
            f"{dominant['factor']} (value {dominant['value']:.2f}, "
            f"contributed {dominant['contribution']:.1f} pts)."
        )
        if floor_applied:
            base += (
                " Life-safety hazard flag present: score floored at 85 "
                "regardless of the weighted sum."
            )
        return base[:400]


def _age_days(first_reported_at: str) -> float:
    """Days since `first_reported_at` (an RFC 3339 timestamp)."""
    reported = dt.datetime.fromisoformat(first_reported_at.replace("Z", "+00:00"))
    return max(0.0, (dt.datetime.now(dt.UTC) - reported).total_seconds() / 86400.0)
