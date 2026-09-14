"""Sentinel L2 — invariant / business rules (PRD section 8.1).

Runs after L1 passes (an envelope L1 already rejected cannot be reasoned about
at all — see `layers/structural.py`). Dispatches to the per-agent rule module
in `agents/av_sentinel/rules/` for the seven concrete topics that have
invariants defined: `reports.ingested` (A0), `reports.understood` (A1),
`reports.linked` (A2), `incidents.updated` (A3), `incidents.prioritized`
(A4), and `incidents.routed` / `incidents.unrouted` (A5).

Every other topic — `reports.rejected`, `evidence.attached`,
`resolution.*`, the skip/quarantine/control families, and Sentinel's own
excluded outputs — has no L2 rules and is reported as such rather than given a
rubber-stamp `pass`: `has_rules_for(topic)` says which is which, and Sentinel
only records and publishes an L2 verdict when it does. **A6 and A7 rows from
PRD 8.1 are not covered at all**, because those agents do not exist yet (P5,
P6) — there is nothing to verify an invariant against.

Severity aggregation follows `agents.av_sentinel.rules.Severity`: the worst
finding across every applicable check decides the verdict, in the order
`fail_hard` > `fail_soft` > `warn` > `pass`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from agents.av_sentinel.layers.structural import Reason
from agents.av_sentinel.rules import Finding, a0, a1, a2, a3, a4, a5

__all__ = ["LAYER", "InvariantVerdict", "has_rules_for", "verify_invariants"]

LAYER: Literal["L2"] = "L2"

Verdict = Literal["pass", "warn", "fail_soft", "fail_hard"]

#: Worst-first precedence used to pick the overall verdict from every finding.
_SEVERITY_ORDER: tuple[Verdict, ...] = ("fail_hard", "fail_soft", "warn")

#: One rule module per topic that has PRD 8.1 invariants defined. A6/A7 are
#: deliberately absent (see module docstring).
_RULES: tuple[Any, ...] = (a0, a1, a2, a3, a4, a5)

_TOPICS_WITH_RULES: frozenset[str] = frozenset(
    {
        "reports.ingested",
        "reports.understood",
        "reports.linked",
        "incidents.updated",
        "incidents.prioritized",
        "incidents.routed",
        "incidents.unrouted",
    }
)


def has_rules_for(topic: str) -> bool:
    """True if L2 has at least one invariant defined for `topic`."""
    return topic in _TOPICS_WITH_RULES


@dataclass(frozen=True, slots=True)
class InvariantVerdict:
    """The outcome of L2 for one envelope."""

    verdict: Verdict
    reasons: list[Reason] = field(default_factory=list)
    layer: Literal["L2"] = LAYER

    @property
    def passed(self) -> bool:
        """True if downstream may proceed: `pass` or `warn`, not soft/hard fail."""
        return self.verdict in ("pass", "warn")

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "layer": self.layer,
            "reasons": [r.to_dict() for r in self.reasons],
        }


def verify_invariants(
    topic: str, envelope: Mapping[str, Any], *, allow_db: bool
) -> InvariantVerdict:
    """Run every applicable L2 rule against one envelope.

    `allow_db` gates the invariants in `rules/` that need state beyond this
    single envelope (an incident's other members, a previous score, whether an
    id has been seen before). Pass `Sentinel.persist` through unchanged: a
    Sentinel with no database can run the self-contained checks but must not
    guess at the ones that need one.
    """
    findings: list[Finding] = []
    for module in _RULES:
        findings.extend(module.check(topic, envelope, allow_db=allow_db))

    if not findings:
        return InvariantVerdict(verdict="pass")

    worst: Verdict = next(
        (s for s in _SEVERITY_ORDER if any(f.severity == s for f in findings)), "warn"
    )
    reasons = [Reason(code=f.code, message=f.message, path=f.path) for f in findings]
    return InvariantVerdict(verdict=worst, reasons=reasons)
