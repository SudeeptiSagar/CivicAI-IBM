"""Publish one reports.ingested event.

A development harness standing in for A0, which lands in P2. M0's acceptance
criterion needs something to start a trace; this is the smallest honest thing
that can: it mints a well-formed envelope and publishes it. It performs none of
A0's real work — no media handling, no EXIF stripping, no reverse geocoding, no
rate limiting — and it is not wired into the API.

    python -m scripts.emit_report
    python -m scripts.emit_report --lat 12.9345 --lon 77.6101 --text "pothole"

Prints the trace_id, which you can follow:

    curl localhost:8000/v1/trace/<trace_id>
"""

from __future__ import annotations

import argparse
import sys

from bus.base import Bus
from bus.factory import make_bus
from common.envelope import Envelope, Producer, utcnow
from common.ids import uuid7
from common.logging import configure_logging
from common.messagelog import archive

# Koramangala 5th Block - the ward the PRD's drainage scenario is seeded in.
DEFAULT_LAT = 12.9345
DEFAULT_LON = 77.6101


def build(lat: float, lon: float, text: str, device: str) -> Envelope:
    """A valid reports.ingested envelope starting a fresh trace."""
    report_id = str(uuid7())
    return Envelope.originate(
        topic="reports.ingested",
        correlation_id=report_id,
        producer=Producer(agent="A0", version="0.0.1", model=None),
        confidence=1.0,
        rationale="Development harness submission (scripts.emit_report), not real intake.",
        payload={
            "report_id": report_id,
            "device_hash": device,
            "created_at": utcnow().isoformat().replace("+00:00", "Z"),
            "location": {"lat": lat, "lon": lon},
            "gps_accuracy_m": 8.5,
            "ward_id": "BLR-151",
            "media_keys": [],
            "raw_text": text,
            "has_photo": False,
            "has_audio": False,
            "lang_hint": "en",
            "source": "api",
        },
    )


def emit(bus: Bus, envelope: Envelope) -> str:
    """Archive then publish, the same ordering every agent uses.

    Publishing without archiving would leave the trace with no root: the
    trace viewer reads `messages`, not the stream.
    """
    archive(envelope)
    return bus.publish(envelope.topic, envelope.to_dict())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT)
    parser.add_argument("--lon", type=float, default=DEFAULT_LON)
    parser.add_argument("--text", default="Large pothole outside the school gate")
    parser.add_argument("--device", default="sha256:devharness")
    parser.add_argument("--count", type=int, default=1, help="number of reports to publish")
    args = parser.parse_args(argv)

    configure_logging()
    bus = make_bus()

    for _ in range(args.count):
        envelope = build(args.lat, args.lon, args.text, args.device)
        emit(bus, envelope)
        print(f"published {envelope.topic} trace_id={envelope.trace_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
