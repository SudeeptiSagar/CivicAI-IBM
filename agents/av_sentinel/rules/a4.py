"""A4 invariants (PRD section 8.1).

PRD row: "Score ∈ [0,100]; breakdown weights sum to 1.0 ± 0.01; recomputed
score matches emitted score; life-safety floor honoured; score monotonic
w.r.t. added corroboration."

The range check is already L1's job (`schemas/incidents.prioritized.v1.json`
bounds `priority_score`); it is not repeated here. The other four:

* **Weights sum to 1.0 ± 0.01** and **life-safety floor honoured** are
  self-contained — `factor_breakdown[].weight` and `life_safety_floor_applied`
  both ride on the wire.
* **Recomputed score matches emitted score** is self-contained from
  `factor_breakdown[].contribution`, *except* when the life-safety floor was
  applied — the floor overrides the weighted sum by definition
  (`agents.a4_priority.apply_life_safety_floor`), so checking arithmetic
  equality in that case would be checking the wrong thing.
* **Score monotonic w.r.t. added corroboration** needs this incident's
  previous score, which is not on this envelope, and there is no
  `priority_history` table (P4 does not add one — see ROADMAP). It is not
  implemented; `agents/av_sentinel/layers/invariants.py` and `docs/sentinel.md`
  say so rather than silently skip it as if it were merely DB-gated like the
  others.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents.a4_priority import LIFE_SAFETY_FLOOR
from agents.av_sentinel.rules import Finding

__all__ = ["check"]

#: PRD section 7/A4 tolerance on the weight sum.
WEIGHT_SUM_TOLERANCE = 0.01

#: Rounding slack for `contribution` values, which the factory and the agent
#: both round to 4 decimal places before summing.
SCORE_TOLERANCE = 0.5


def check(topic: str, envelope: Mapping[str, Any], *, allow_db: bool) -> list[Finding]:
    if topic != "incidents.prioritized":
        return []
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return []

    findings: list[Finding] = []
    findings.extend(_weights_sum_to_one(payload))
    findings.extend(_life_safety_floor_honoured(payload))
    findings.extend(_recomputed_score_matches(payload))
    return findings


def _breakdown(payload: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    breakdown = payload.get("factor_breakdown")
    if not isinstance(breakdown, list) or not breakdown:
        return None
    if not all(isinstance(item, Mapping) for item in breakdown):
        return None
    return breakdown


def _weights_sum_to_one(payload: Mapping[str, Any]) -> list[Finding]:
    breakdown = _breakdown(payload)
    if breakdown is None:
        return []
    raw_weights = [item.get("weight") for item in breakdown]
    if not all(isinstance(w, int | float) for w in raw_weights):
        return []
    weights: list[float] = [float(w) for w in raw_weights if isinstance(w, int | float)]
    total = sum(weights, 0.0)
    if abs(total - 1.0) <= WEIGHT_SUM_TOLERANCE:
        return []
    return [
        Finding(
            code="a4_weights_do_not_sum_to_one",
            message=(
                f"factor_breakdown weights sum to {total:.4f}, "
                f"expected 1.0 +/- {WEIGHT_SUM_TOLERANCE}"
            ),
            severity="fail_hard",
            path="payload.factor_breakdown",
        )
    ]


def _life_safety_floor_honoured(payload: Mapping[str, Any]) -> list[Finding]:
    floor_applied = payload.get("life_safety_floor_applied")
    score = payload.get("priority_score")
    if floor_applied is not True or not isinstance(score, int | float):
        return []
    if float(score) >= LIFE_SAFETY_FLOOR:
        return []
    return [
        Finding(
            code="a4_life_safety_floor_not_honoured",
            message=(
                f"life_safety_floor_applied is true but priority_score is "
                f"{score}, below the {LIFE_SAFETY_FLOOR} floor"
            ),
            severity="fail_hard",
            path="payload.priority_score",
        )
    ]


def _recomputed_score_matches(payload: Mapping[str, Any]) -> list[Finding]:
    if payload.get("life_safety_floor_applied") is True:
        # The floor overrides the weighted sum by definition; arithmetic
        # equality is the wrong test here (see module docstring).
        return []
    breakdown = _breakdown(payload)
    score = payload.get("priority_score")
    if breakdown is None or not isinstance(score, int | float):
        return []
    raw_contributions = [item.get("contribution") for item in breakdown]
    if not all(isinstance(c, int | float) for c in raw_contributions):
        return []
    contributions: list[float] = [float(c) for c in raw_contributions if isinstance(c, int | float)]
    recomputed = sum(contributions, 0.0)
    if abs(recomputed - float(score)) <= SCORE_TOLERANCE:
        return []
    return [
        Finding(
            code="a4_score_does_not_match_breakdown",
            message=(
                f"priority_score is {score} but factor_breakdown contributions "
                f"sum to {recomputed:.4f}"
            ),
            severity="fail_hard",
            path="payload.priority_score",
        )
    ]
