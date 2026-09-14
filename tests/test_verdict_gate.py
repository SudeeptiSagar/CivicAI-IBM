"""The Sentinel verdict gate (PRD section 8.3, P4).

Runs entirely against a fake `VerdictStore` and a fake sleep function, so mode
switching, the deadline and the decision logic are covered without a database
or the wall-clock delay a real 2-second deadline would cost every test run.
`tests/test_agent_base.py` covers the gate wired into `Agent`; this file is
the gate's own contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from common.verdict_gate import GateResult, VerdictGate

# -- a controllable fake store --------------------------------------------


@dataclass
class FakeStore:
    """Returns a scripted verdict, optionally only after N lookups.

    `arrives_after` models "the verdict shows up partway through the wait":
    the first `arrives_after` calls return None, then `verdict` forever.
    """

    verdict: str | None
    arrives_after: int = 0
    calls: int = field(default=0, init=False)

    def get_verdict(self, message_id: str) -> str | None:
        self.calls += 1
        if self.verdict is None:
            return None
        if self.calls <= self.arrives_after:
            return None
        return self.verdict


class FakeClock:
    """A fake monotonic clock plus a sleep that advances it.

    `time.monotonic` is patched module-wide in `common.verdict_gate` via
    monkeypatch in each test that needs deadline behaviour, so the deadline
    math runs against virtual time instead of the wall clock.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr("common.verdict_gate.time.monotonic", fake.monotonic)
    return fake


# -- shadow mode: never waits, never blocks --------------------------------


def test_shadow_mode_proceeds_immediately_with_no_verdict() -> None:
    gate = VerdictGate(FakeStore(verdict=None), mode="shadow")
    result = gate.await_verdict("m1")

    assert result == GateResult(decision="proceed", verdict=None, waited_ms=0.0, timed_out=False)


def test_shadow_mode_proceeds_even_on_fail_hard() -> None:
    """Shadow never blocks, even if Sentinel already failed the message."""
    gate = VerdictGate(FakeStore(verdict="fail_hard"), mode="shadow")
    result = gate.await_verdict("m1")

    assert result.decision == "proceed"


def test_shadow_mode_never_calls_the_store() -> None:
    store = FakeStore(verdict=None)
    VerdictGate(store, mode="shadow").await_verdict("m1")

    assert store.calls == 0


# -- an immediate verdict --------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "permissive"])
@pytest.mark.parametrize("verdict", ["pass", "warn"])
def test_pass_or_warn_proceeds_immediately(clock: FakeClock, mode: str, verdict: str) -> None:
    gate = VerdictGate(
        FakeStore(verdict=verdict),
        mode=mode,  # type: ignore[arg-type]
        deadline_ms=2000,
        sleep=clock.sleep,
    )
    result = gate.await_verdict("m1")

    assert result.decision == "proceed"
    assert result.verdict == verdict
    assert result.timed_out is False
    assert clock.slept == []


@pytest.mark.parametrize("mode", ["strict", "permissive"])
@pytest.mark.parametrize("verdict", ["fail_soft", "fail_hard"])
def test_fail_verdict_drops_immediately(clock: FakeClock, mode: str, verdict: str) -> None:
    """Sentinel has already reacted (retry or quarantine) - the consumer must
    not act on this delivery, in either blocking mode."""
    gate = VerdictGate(
        FakeStore(verdict=verdict),
        mode=mode,  # type: ignore[arg-type]
        deadline_ms=2000,
        sleep=clock.sleep,
    )
    result = gate.await_verdict("m1")

    assert result.decision == "drop"
    assert result.verdict == verdict


# -- the verdict arrives partway through the wait ---------------------------


def test_verdict_arriving_during_the_wait_is_picked_up(clock: FakeClock) -> None:
    store = FakeStore(verdict="pass", arrives_after=3)
    gate = VerdictGate(
        store, mode="strict", deadline_ms=2000, poll_interval_ms=100, sleep=clock.sleep
    )

    result = gate.await_verdict("m1")

    assert result.decision == "proceed"
    assert result.timed_out is False
    # Polled a few times, slept a few times, and stopped well inside the deadline.
    assert 0 < len(clock.slept) < 20
    assert clock.now < 2.0


# -- the deadline ------------------------------------------------------------


def test_strict_defers_when_the_deadline_lapses_with_no_verdict(clock: FakeClock) -> None:
    gate = VerdictGate(
        FakeStore(verdict=None),
        mode="strict",
        deadline_ms=200,
        poll_interval_ms=50,
        sleep=clock.sleep,
    )
    result = gate.await_verdict("m1")

    assert result.decision == "defer"
    assert result.verdict is None
    assert result.timed_out is True
    assert clock.now >= 0.2


def test_permissive_proceeds_when_the_deadline_lapses_with_no_verdict(clock: FakeClock) -> None:
    gate = VerdictGate(
        FakeStore(verdict=None),
        mode="permissive",
        deadline_ms=200,
        poll_interval_ms=50,
        sleep=clock.sleep,
    )
    result = gate.await_verdict("m1")

    assert result.decision == "proceed"
    assert result.verdict is None
    assert result.timed_out is True


def test_poll_interval_does_not_overshoot_the_deadline(clock: FakeClock) -> None:
    """The last sleep must not sleep past the deadline by a whole poll interval,
    or a strict wait configured for 2s could cost noticeably more."""
    gate = VerdictGate(
        FakeStore(verdict=None),
        mode="strict",
        deadline_ms=120,
        poll_interval_ms=100,
        sleep=clock.sleep,
    )
    gate.await_verdict("m1")

    # Never sleeps more than the remaining time to the deadline in one step.
    assert all(s <= 0.12 for s in clock.slept)


# -- defaults come from settings, and can be overridden ----------------------


def test_defaults_come_from_settings() -> None:
    gate = VerdictGate(FakeStore(verdict=None))
    assert gate.mode in {"strict", "permissive", "shadow"}
    assert gate.deadline_ms == 2000


def test_explicit_mode_overrides_settings() -> None:
    gate = VerdictGate(FakeStore(verdict=None), mode="shadow")
    assert gate.mode == "shadow"
