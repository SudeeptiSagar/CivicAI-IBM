"""Per-agent invariant definitions consumed by Sentinel L2 (P4, PRD section 8.1).

Each module here (`a0` .. `a5`) covers the "sample invariants" row for one
producing agent from the PRD 8.1 table, and exposes one function:

    check(topic: str, envelope: Mapping[str, Any], *, allow_db: bool) -> list[Finding]

`allow_db` is threaded down from `Sentinel.persist`. Some invariants in the PRD
table need state this single envelope does not carry (an incident's other
members, a report's prior existence, a previous score to check monotonicity
against) — those checks run only when a database is actually reachable, and are
silently skipped otherwise rather than guessed at. A skipped check is not a
passed check; see `agents/av_sentinel/layers/invariants.py` for how that is
reported.

**A6 and A7 are not covered.** Those agents do not exist yet (P5 and P6), so
there is nothing to check an invariant against — a rule with no implementation
behind it would be exactly the kind of overstated coverage this repo tries not
to ship. `docs/sentinel.md` and `ROADMAP.md` say so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Finding", "Severity"]

#: A finding's severity maps directly onto a Sentinel verdict once aggregated:
#: fail_hard beats fail_soft beats warn beats a clean pass. The classification
#: used throughout `rules/`: a violation that proves the agent's own arithmetic
#: or bookkeeping is wrong (a count that does not match its members, weights
#: that do not sum to one, a department outside the registry) is `fail_hard` —
#: nothing downstream can trust the message. A violation that looks like a
#: quality problem in otherwise-coherent output (a summary over the word cap)
#: is `fail_soft` — worth one retry by the producer. A violation that rests on
#: a heuristic re-check rather than a recomputation of the agent's own logic
#: (the modality-conflict guess in `a1.py`) is `warn` — worth surfacing, not
#: worth blocking on.
Severity = Literal["warn", "fail_soft", "fail_hard"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One specific invariant violation."""

    code: str
    message: str
    severity: Severity
    path: str | None = None
