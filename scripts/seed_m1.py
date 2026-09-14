"""Load and run the M1 seed scenario (PRD section 13, M1).

    python -m scripts.seed_m1              publish the 20 reports
    python -m scripts.seed_m1 --drain      publish, then run the agents to completion

`--drain` runs A1, A2 and A3 in-process until their topics are empty, which is
how the test harness and a local demo get a deterministic end state without
racing the long-running containers. In the deployed stack the agents consume
continuously and no draining is needed.

Reports are submitted through `IntakeAgent` — the real A0 — so the seeded data
goes through the same validation, ward lookup and rate limiting as a citizen
submission. Only the transport differs: no HTTP, no media.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Any

from agents.a0_intake import IntakeAgent, Submission
from bus.base import Bus
from common.logging import configure_logging, get_logger

__all__ = ["SEED_FILE", "drain", "load_scenario", "seed"]

log = get_logger(__name__)

SEED_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / "seed" / "m1_reports.json"

#: The seeded devices submit more than the per-device hourly limit in one burst
#: only if a cluster reuses a device; they do not. Kept explicit so a future
#: fixture that does trips this comment rather than the rate limiter.
_EXPECTED_MAX_PER_DEVICE = 2


def load_scenario(path: pathlib.Path = SEED_FILE) -> dict[str, Any]:
    """The scenario file, parsed."""
    scenario: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return scenario


def seed(bus: Bus, scenario: dict[str, Any] | None = None) -> list[str]:
    """Submit every seeded report through A0. Returns the report ids."""
    scenario = scenario or load_scenario()
    intake = IntakeAgent(bus)
    now = dt.datetime.now(dt.UTC)
    report_ids: list[str] = []

    for entry in scenario["reports"]:
        result = intake.intake(
            Submission(
                device_hash=entry["device"],
                lat=entry["lat"],
                lon=entry["lon"],
                gps_accuracy_m=float(entry["accuracy_m"]),
                text=entry["text"],
                language_hint="en",
                source="api",
            )
        )
        if not result.accepted:
            raise RuntimeError(
                f"seed report {entry['id']!r} was rejected: {result.reason_code} - {result.reason}"
            )
        report_ids.append(str(result.report_id))

        # A0 stamps created_at at intake. The scenario's hours_ago offsets are
        # what make the temporal component of dedup meaningful, so they are
        # applied here rather than faked inside the agent.
        _backdate(str(result.report_id), now - dt.timedelta(hours=entry["hours_ago"]))

    log.info("seeded reports", extra={"count": len(report_ids)})
    return report_ids


def _backdate(report_id: str, created_at: dt.datetime) -> None:
    """Move a seeded report back in time.

    Test-fixture surgery, confined to this script: no agent may rewrite a
    report's created_at, which is why this is here and not in A0.
    """
    from common.db import transaction

    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE reports SET created_at = %s WHERE report_id = %s",
            (created_at, report_id),
        )


def drain(bus: Bus, *, max_rounds: int = 40) -> dict[str, int]:
    """Run A1, A2 and A3 until their topics are empty.

    Ordered deliberately: A2 scores a report against incidents A3 has already
    created, so draining A1 fully before A2, and A2 before A3, would make every
    report see an empty incident table and seed its own. Cycling one message at
    a time through all three mirrors how the deployed agents interleave.
    """
    from agents.a1_perception import PerceptionAgent
    from agents.a2_dedup import DedupAgent
    from agents.a3_synthesis import SynthesisAgent

    perception = PerceptionAgent(bus, consumer="seed-a1")
    dedup = DedupAgent(bus, consumer="seed-a2")
    synthesis = SynthesisAgent(bus, consumer="seed-a3")

    for agent in (perception, dedup, synthesis):
        bus.create_group(agent.input_topic, agent.group)

    handled = {"A1": 0, "A2": 0, "A3": 0}

    for _ in range(max_rounds):
        progressed = False
        for key, agent in (("A1", perception), ("A2", dedup), ("A3", synthesis)):
            results = agent.run_once(count=1)
            if results:
                handled[key] += len(results)
                progressed = True
        if not progressed:
            break

    log.info("drained", extra=handled)
    return handled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drain", action="store_true", help="run A1/A2/A3 in-process after seeding"
    )
    args = parser.parse_args(argv)

    configure_logging()

    from bus.factory import make_bus

    bus = make_bus()
    scenario = load_scenario()

    # Groups must exist before publishing, or a consumer starting at the tail
    # will never see the seeded backlog.
    if args.drain:
        from agents.a1_perception import PerceptionAgent
        from agents.a2_dedup import DedupAgent
        from agents.a3_synthesis import SynthesisAgent

        for agent_type in (PerceptionAgent, DedupAgent, SynthesisAgent):
            probe = agent_type(bus)
            bus.create_group(probe.input_topic, probe.group)

    report_ids = seed(bus, scenario)
    print(f"seeded {len(report_ids)} reports")

    if args.drain:
        handled = drain(bus)
        print(f"drained: {handled}")
        print(f"expected incidents: {len(scenario['expected_incidents'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
