"""Report-to-incident matching (PRD section 7/A2).

Shared by A2 and A3, and shared deliberately.

A2 *proposes* a link by scoring a report against the incidents that exist when
it looks. A3 *disposes*: it owns the `incidents` table, so its view is the
authoritative one. Those two moments are not the same moment, and under load
they can disagree — which is the reason this module exists rather than the
scoring living inside A2.

## The race this closes

Agents run concurrently, and PRD section 9.4 guarantees ordering per
`correlation_id` only. Four citizens reporting one pothole within a second
produce four reports with four different correlation ids, so A2 can score all
four before A3 has created an incident for the first. Every one of them sees an
empty incident table, every one seeds, and the demo that was supposed to show
four reports collapsing into one shows four incidents.

So A3 re-runs the same scoring against current state before seeding anything
new. Same function, same thresholds, later and authoritative. A2's proposal is
still what carries the reasoning downstream; A3's re-check only ever converts a
seed into a join, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "AUTO_LINK_THRESHOLD",
    "CANDIDATE_THRESHOLD",
    "CATEGORY_RADIUS_M",
    "CATEGORY_WINDOW_DAYS",
    "DEFAULT_RADIUS_M",
    "DEFAULT_WINDOW_DAYS",
    "WEIGHTS",
    "Candidate",
    "bounds_for",
    "score_candidates",
]

#: PRD section 7/A2 thresholds.
AUTO_LINK_THRESHOLD: Final = 0.82
CANDIDATE_THRESHOLD: Final = 0.60

#: Component weights. The PRD names the four components and the formula but not
#: the weights; these put the two deterministic signals (space, time) at half
#: the total, so a confident geo/time match plus weak text still clears the
#: candidate band while never auto-linking on text alone.
WEIGHTS: Final[dict[str, float]] = {
    "spatial": 0.35,
    "temporal": 0.15,
    "semantic": 0.35,
    "visual": 0.15,
}

#: Radii from PRD section 7/A2 where given (pothole 75, garbage 150,
#: waterlogging 300); the rest follow the same logic — how far apart two
#: reports of the same thing can plausibly be pinned.
CATEGORY_RADIUS_M: Final[dict[str, float]] = {
    "pothole": 75.0,
    "garbage": 150.0,
    "waterlogging": 300.0,
    "drain_overflow": 200.0,
    "sewage": 200.0,
    "road_damage": 100.0,
    "streetlight": 60.0,
    "water_supply": 200.0,
    "tree_hazard": 80.0,
    "stray_animals": 300.0,
    "traffic_obstruction": 150.0,
    "public_toilet": 50.0,
    "encroachment": 80.0,
    "other": 100.0,
}

#: Windows from PRD section 7/A2 where given (pothole 21 d, waterlogging 3 d,
#: garbage 7 d). A window is how long the same physical problem stays "the same
#: report" — waterlogging drains, a pothole does not.
CATEGORY_WINDOW_DAYS: Final[dict[str, float]] = {
    "pothole": 21.0,
    "garbage": 7.0,
    "waterlogging": 3.0,
    "drain_overflow": 5.0,
    "sewage": 7.0,
    "road_damage": 30.0,
    "streetlight": 30.0,
    "water_supply": 3.0,
    "tree_hazard": 14.0,
    "stray_animals": 7.0,
    "traffic_obstruction": 2.0,
    "public_toilet": 14.0,
    "encroachment": 30.0,
    "other": 7.0,
}

DEFAULT_RADIUS_M: Final = 100.0
DEFAULT_WINDOW_DAYS: Final = 7.0


def bounds_for(category: str) -> tuple[float, float]:
    """The (radius_m, window_days) a category is deduplicated within."""
    return (
        CATEGORY_RADIUS_M.get(category, DEFAULT_RADIUS_M),
        CATEGORY_WINDOW_DAYS.get(category, DEFAULT_WINDOW_DAYS),
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    """One incident a report might belong to."""

    incident_id: str
    spatial: float
    temporal: float
    semantic: float
    visual: float | None

    @property
    def score(self) -> float:
        """Weighted sum over the components that are actually available.

        An unavailable component is renormalised away rather than scored as
        zero. Scoring it zero would drag every match below the threshold and
        defeat dedup entirely — the visual component is unavailable for any
        pair where either side has no photo, which in this build is every pair.
        """
        components = {
            "spatial": self.spatial,
            "temporal": self.temporal,
            "semantic": self.semantic,
            "visual": self.visual,
        }
        available = {k: v for k, v in components.items() if v is not None}
        total_weight = sum(WEIGHTS[k] for k in available)
        if total_weight == 0:
            return 0.0
        return round(sum(WEIGHTS[k] * v for k, v in available.items()) / total_weight, 6)

    def component_scores(self) -> dict[str, float | None]:
        return {
            "spatial": self.spatial,
            "temporal": self.temporal,
            "semantic": self.semantic,
            "visual": self.visual,
        }


def score_candidates(
    report_id: str, category: str, radius_m: float, window_days: float
) -> list[Candidate]:
    """Score every open incident of the same category within radius and window.

    One query: PostGIS filters by distance, pgvector supplies cosine distance
    against the incident's member reports, and the category equality is a hard
    predicate so a cross-category merge is never even considered
    (PRD section 7/A2).
    """
    from common.db import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH subject AS (
                SELECT geom, embedding, created_at, media_keys
                FROM reports
                WHERE report_id = %(report_id)s
            )
            SELECT
                i.incident_id,
                ST_Distance(i.centroid, s.geom)                        AS distance_m,
                EXTRACT(EPOCH FROM (s.created_at - i.last_reported_at))
                    / 86400.0                                          AS age_days,
                (
                    SELECT min(r.embedding <=> s.embedding)
                    FROM reports r
                    WHERE r.incident_id = i.incident_id
                      AND r.embedding IS NOT NULL
                )                                                      AS cosine_distance
            FROM incidents i, subject s
            WHERE i.category = %(category)s
              AND i.status <> 'verified'
              AND ST_DWithin(i.centroid, s.geom, %(radius)s)
              AND s.created_at >= i.first_reported_at - make_interval(days => %(window)s::int)
              AND s.created_at <= i.last_reported_at + make_interval(days => %(window)s::int)
            """,
            {
                "report_id": report_id,
                "category": category,
                "radius": radius_m,
                "window": int(window_days),
            },
        )
        rows = [dict(row) for row in cur.fetchall()]

    return [_score_row(row, radius_m, window_days) for row in rows]


def _score_row(row: dict[str, Any], radius_m: float, window_days: float) -> Candidate:
    # Linear falloff to the category radius: touching distance scores 1, the
    # edge of the radius scores 0.
    distance = float(row["distance_m"])
    spatial = max(0.0, 1.0 - distance / radius_m) if radius_m > 0 else 0.0

    # Same shape over the category window, on absolute age: a report can arrive
    # before an incident's last report as easily as after.
    age = abs(float(row["age_days"] or 0.0))
    temporal = max(0.0, 1.0 - age / window_days) if window_days > 0 else 0.0

    # pgvector's <=> is cosine *distance*; similarity is 1 - distance, clamped
    # because a lexical vector can land fractionally outside [0,1].
    cosine_distance = row["cosine_distance"]
    semantic = (
        max(0.0, min(1.0, 1.0 - float(cosine_distance))) if cosine_distance is not None else 0.0
    )

    # Visual needs both sides to have a photo *and* a provider able to compare
    # them. No vision provider exists in this build, so it stays null and its
    # weight is redistributed.
    return Candidate(
        incident_id=str(row["incident_id"]),
        spatial=round(spatial, 6),
        temporal=round(temporal, 6),
        semantic=round(semantic, 6),
        visual=None,
    )
