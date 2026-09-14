"""The Sentinel verdict gate (PRD section 8.3, P4).

Closes ROADMAP's caveat 3: before P4, Sentinel and each consuming agent held
independent consumer groups on the same topic, so an agent could act on a
message Sentinel was about to quarantine. The gate makes a consumer wait for
Sentinel's verdict on a specific message before it is allowed to proceed.

## Design

The gate does not subscribe to `verification.results` on the bus. Doing that
per-message, per-agent would mean every agent maintains a second consumer
group and matches incoming verdicts back to the message it is waiting on —
plausible, but it duplicates the durable store Sentinel already writes to
(`verification_results`, PRD section 10) and adds a second source of truth for
"has this been verified yet". Instead the gate polls that table directly by
`message_id`. This is also what makes the gate bus-agnostic: `bus/memory.py`
and `bus/redis.py` need no changes at all, and the same gate logic runs
identically against either.

The poll interval (`Settings.sentinel_gate_poll_ms`, default 50ms) is a plain
`time.sleep` loop bounded by the deadline — not a blocking bus read and not a
tight spin — so 2 seconds of waiting costs at most ~40 small SELECTs, not a
busy loop. A consumer that has no database (`persist=False`, every unit test
today) cannot use `PostgresVerdictStore`; `agents.base.Agent` only constructs
the gate when `persist=True` and the flag is on (see its wiring).

## Modes (PRD section 8.3)

* `strict` — wait up to the deadline for `pass` or `warn`. No verdict yet at
  the deadline: do not proceed (the caller nacks/defers, see `agents/base.py`).
  A `fail_hard` or `fail_soft` verdict found within the deadline: do not
  proceed, ever, for this delivery — Sentinel has already reacted to it
  (quarantine, or a producer retry), and the consumer acting on it would be
  exactly the race PRD 8 describes.
* `permissive` — same as `strict` up to the deadline, but at the deadline with
  still no verdict, proceed anyway. The gap is logged (`gate_timeout` event)
  and counted so it is visible, not just silently allowed.
* `shadow` — never waits and never blocks. Verdicts are still recorded by
  Sentinel independently; the gate's job in this mode is only to say "proceed"
  immediately, so it can be swapped in for calibration without touching
  `agents/base.py` again.

## What this does not cover

The PRD's fallback #2 ("Sentinel verifying and forwarding onto a separate
verified topic") is not what this implements — that would change every
agent's input topic, which is the larger refactor `docs/sentinel.md`
explicitly chose against in P1. This gate is fallback #1: consumers wait for
a verdict on `verification.results` (here, its durable store) before acting.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from common.config import SentinelMode, settings
from common.logging import get_logger

__all__ = [
    "GateDecision",
    "GateResult",
    "PostgresVerdictStore",
    "VerdictGate",
    "VerdictStore",
]

log = get_logger(__name__)

#: What the gate tells the caller to do with this delivery.
GateDecision = Literal["proceed", "defer", "drop"]

#: A plain callable, typed loosely so tests can inject a fake without pulling
#: in threading.Event machinery just to skip real sleeps.
_SleepFn = Callable[[float], None]


class VerdictStore(Protocol):
    """Looks up the latest terminal verdict for one message, if any.

    "Terminal" means the aggregate outcome a consumer cares about, not every
    individual layer's row: `pass`/`warn` (proceed), `fail_soft`/`fail_hard`
    (do not — Sentinel has already reacted), or `None` (no verdict yet).
    """

    def get_verdict(self, message_id: str) -> str | None: ...


class PostgresVerdictStore:
    """Reads `verification_results` directly (PRD section 10).

    Picks the *worst* verdict recorded across layers for this message, not the
    latest row: a message with an L1 `pass` and an L2 `fail_hard` has failed,
    regardless of which layer finished first.
    """

    _PRECEDENCE = ("fail_hard", "fail_soft", "warn", "pass")

    def get_verdict(self, message_id: str) -> str | None:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT verdict FROM verification_results WHERE message_id = %s", (message_id,)
            )
            rows = cur.fetchall()
        if not rows:
            return None
        seen = {str(row["verdict"]) for row in rows}
        return next((v for v in self._PRECEDENCE if v in seen), None)


@dataclass(frozen=True, slots=True)
class GateResult:
    """What the gate decided, and why — for logging and tests."""

    decision: GateDecision
    verdict: str | None
    waited_ms: float
    timed_out: bool


class VerdictGate:
    """Waits for a Sentinel verdict on one message, per PRD section 8.3."""

    def __init__(
        self,
        store: VerdictStore,
        *,
        mode: SentinelMode | None = None,
        deadline_ms: int | None = None,
        poll_interval_ms: int | None = None,
        sleep: _SleepFn | None = None,
    ) -> None:
        cfg = settings()
        self.store = store
        self.mode: SentinelMode = mode or cfg.sentinel_mode
        self.deadline_ms = deadline_ms if deadline_ms is not None else cfg.sentinel_deadline_ms
        self.poll_interval_ms = (
            poll_interval_ms if poll_interval_ms is not None else cfg.sentinel_gate_poll_ms
        )
        self._sleep = sleep or time.sleep

    def await_verdict(self, message_id: str) -> GateResult:
        """Block (briefly, and only in `strict`/`permissive`) for a verdict."""
        if self.mode == "shadow":
            # Sentinel still verifies independently; the gate just never blocks.
            return GateResult(decision="proceed", verdict=None, waited_ms=0.0, timed_out=False)

        started = time.monotonic()
        deadline = started + self.deadline_ms / 1000.0

        while True:
            verdict = self.store.get_verdict(message_id)
            if verdict is not None:
                waited_ms = (time.monotonic() - started) * 1000.0
                return self._decide(verdict, waited_ms, timed_out=False)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep(min(self.poll_interval_ms / 1000.0, remaining))

        waited_ms = (time.monotonic() - started) * 1000.0
        if self.mode == "permissive":
            log.warning(
                "sentinel verdict gate timed out; proceeding (permissive)",
                extra={"message_id": message_id, "waited_ms": round(waited_ms, 1)},
            )
            return GateResult(decision="proceed", verdict=None, waited_ms=waited_ms, timed_out=True)

        # strict: no verdict within the deadline means do not proceed yet.
        # The caller defers this delivery (nack) rather than dropping it —
        # the verdict may simply still be in flight, not wrong.
        log.warning(
            "sentinel verdict gate timed out; deferring (strict)",
            extra={"message_id": message_id, "waited_ms": round(waited_ms, 1)},
        )
        return GateResult(decision="defer", verdict=None, waited_ms=waited_ms, timed_out=True)

    def _decide(self, verdict: str, waited_ms: float, *, timed_out: bool) -> GateResult:
        if verdict in ("pass", "warn"):
            return GateResult(
                decision="proceed", verdict=verdict, waited_ms=waited_ms, timed_out=timed_out
            )
        # fail_soft or fail_hard: Sentinel has already reacted to this specific
        # message (a producer retry, or quarantine). This delivery is done.
        return GateResult(
            decision="drop", verdict=verdict, waited_ms=waited_ms, timed_out=timed_out
        )
