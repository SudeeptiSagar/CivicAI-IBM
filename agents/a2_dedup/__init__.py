"""A2 — Deduplication (PRD section 7/A2).

**Job:** "Has someone already reported this?"
**In:** `reports.understood`. **Out:** `reports.linked`.

Candidate generation, then scoring on four components:

    spatial   PostGIS ST_DWithin within a category-specific radius
    temporal  within a category-specific window
    semantic  cosine similarity of A1 embeddings (pgvector)
    visual    perceptual hash / image embedding, when both have photos

    match_score = w_s*spatial + w_t*temporal + w_e*semantic + w_v*visual

    >= 0.82          auto-link to the existing incident
    0.60 - 0.82      candidate_link; A3 arbitrates
    <  0.60          new incident seed

The scoring itself lives in `common.matching`, because A3 re-runs it against
authoritative state before seeding anything new — see that module for why.

## Never merges across categories

PRD section 7/A2 is explicit: cross-category association is A3's job. Candidate
generation filters on category, so a waterlogging report can never auto-link to
a pothole incident here no matter how close in space and time.

## A2 proposes, A3 disposes

A2's decision is a proposal scored against the incidents that existed when it
looked. Under concurrency that view can be stale, so A3 confirms before acting.
A2 remains the agent that explains *why*: the component scores and rationale it
publishes are what a reviewer reads.
"""

from __future__ import annotations

from agents.base import Agent
from common.envelope import Envelope
from common.logging import get_logger
from common.matching import (
    AUTO_LINK_THRESHOLD,
    CANDIDATE_THRESHOLD,
    CATEGORY_RADIUS_M,
    CATEGORY_WINDOW_DAYS,
    WEIGHTS,
    Candidate,
    bounds_for,
    score_candidates,
)

__all__ = [
    "AUTO_LINK_THRESHOLD",
    "CANDIDATE_THRESHOLD",
    "CATEGORY_RADIUS_M",
    "CATEGORY_WINDOW_DAYS",
    "WEIGHTS",
    "Candidate",
    "DedupAgent",
]

log = get_logger(__name__)

_NO_SCORES: dict[str, float | None] = {
    "spatial": None,
    "temporal": None,
    "semantic": None,
    "visual": None,
}


class DedupAgent(Agent):
    """Proposes a link to an existing incident, or a new seed."""

    name = "A2"
    version = "1.0.0"
    input_topic = "reports.understood"

    def handle(self, envelope: Envelope) -> list[Envelope]:
        payload = envelope.payload
        report_id = payload["report_id"]
        category = payload["category"]
        radius, window = bounds_for(category)

        candidates = score_candidates(report_id, category, radius, window) if self.persist else []
        best = max(candidates, key=lambda c: c.score, default=None)
        decision, incident_id = self._decide(best)

        log.info(
            "dedup decision",
            extra={
                "report_id": report_id,
                "decision": decision,
                "score": best.score if best else 0.0,
                "candidates": len(candidates),
            },
        )

        return [
            envelope.derive(
                topic="reports.linked",
                producer=self.producer,
                confidence=self._confidence(best, decision),
                rationale=self._rationale(decision, best, category, radius, window, candidates),
                payload={
                    "report_id": report_id,
                    "decision": decision,
                    "incident_id": incident_id,
                    "match_score": best.score if best else 0.0,
                    "component_scores": best.component_scores() if best else dict(_NO_SCORES),
                    "candidates": [
                        {
                            "incident_id": c.incident_id,
                            "score": c.score,
                            "component_scores": c.component_scores(),
                        }
                        for c in sorted(candidates, key=lambda c: c.score, reverse=True)[:5]
                    ],
                    "radius_m": radius,
                    "window_days": window,
                },
            )
        ]

    # -- the decision -----------------------------------------------------

    @staticmethod
    def _decide(best: Candidate | None) -> tuple[str, str | None]:
        if best is None or best.score < CANDIDATE_THRESHOLD:
            return "new_incident_seed", None
        if best.score >= AUTO_LINK_THRESHOLD:
            return "auto_link", best.incident_id
        return "candidate_link", best.incident_id

    @staticmethod
    def _confidence(best: Candidate | None, decision: str) -> float:
        """How sure A2 is of its own decision.

        A score in the middle of the grey zone is the *least* confident state,
        which is exactly why the PRD sends that band to A3 to arbitrate.
        """
        if best is None:
            return 0.9  # nothing nearby is a clear answer
        if decision == "auto_link":
            return round(min(1.0, best.score), 4)
        if decision == "new_incident_seed":
            return round(min(1.0, 1.0 - best.score), 4)
        midpoint = (AUTO_LINK_THRESHOLD + CANDIDATE_THRESHOLD) / 2
        span = (AUTO_LINK_THRESHOLD - CANDIDATE_THRESHOLD) / 2
        return round(0.5 * abs(best.score - midpoint) / span, 4)

    @staticmethod
    def _rationale(
        decision: str,
        best: Candidate | None,
        category: str,
        radius_m: float,
        window_days: float,
        candidates: list[Candidate],
    ) -> str:
        scope = (
            f"Searched {category} incidents within {radius_m:.0f} m and "
            f"{window_days:.0f} d: {len(candidates)} candidate(s)."
        )
        if best is None:
            return f"{scope} Nothing comparable nearby, so this seeds a new incident."

        components = ", ".join(
            f"{name}={value:.2f}" if value is not None else f"{name}=n/a"
            for name, value in best.component_scores().items()
        )
        verdict = {
            "auto_link": f"score {best.score:.2f} >= {AUTO_LINK_THRESHOLD}, auto-linked",
            "candidate_link": (
                f"score {best.score:.2f} is in the "
                f"{CANDIDATE_THRESHOLD}-{AUTO_LINK_THRESHOLD} grey zone; A3 arbitrates"
            ),
            "new_incident_seed": (
                f"best score {best.score:.2f} < {CANDIDATE_THRESHOLD}, so this seeds a new incident"
            ),
        }[decision]
        return f"{scope} Best match {components}; {verdict}."
