"""A0 — Intake / Gateway (PRD section 7/A0).

**Job:** turn a raw citizen submission into a well-formed `Report`.
**In:** HTTP multipart from the PWA. **Out:** `reports.ingested`.

A0 is the only agent that does not consume from the bus — it is the trace
boundary. `POST /v1/reports` calls `intake()` below, which either mints a
report and publishes `reports.ingested`, or publishes `reports.rejected` with a
reason. Both outcomes originate a trace, so even a rejection is auditable.

What it does, per the PRD:

* validates media type and size, strips EXIF except the geotag, stores blobs
* mints `report_id`, resolves GPS to ward and zone by PostGIS polygon join
* rate-limits per device to blunt spam and brigading
* emits `reports.rejected` with a reason on validation failure

What it does not do, and why:

* **No reverse geocoding.** PRD section 7/A0 lists "device GPS -> reverse
  geocode -> ward/zone lookup". Ward lookup is implemented against local
  polygons; reverse geocoding to a street address needs a geocoder this build
  has no provider for. Reports carry coordinates and a ward, not a street name.
* **No face or plate blurring.** PRD section 15 asks for it; it needs a
  detector this build does not have. See docs/media-handling.md.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from bus.base import Bus
from common.db import transaction
from common.envelope import Envelope, Producer, utcnow
from common.geo import Point, inside_city, ward_for
from common.ids import uuid7
from common.logging import bind_trace, get_logger
from common.messagelog import archive
from common.storage import MediaRejected, StoredMedia, store_audio, store_image

__all__ = ["RATE_LIMIT", "RATE_WINDOW", "IntakeAgent", "IntakeResult", "Submission"]

log = get_logger(__name__)

#: PRD section 7/A0 requires a per-device rate limit without setting a number.
#: 10 reports an hour is generous for a citizen and cheap to brigade past only
#: with many devices, which is what the corroboration scaling in A4 handles.
RATE_LIMIT = 10
RATE_WINDOW = dt.timedelta(hours=1)

#: GPS worse than this is treated as unusable for a ward assignment.
MAX_USABLE_ACCURACY_M = 500.0


@dataclass(frozen=True, slots=True)
class Submission:
    """One raw citizen submission, before validation."""

    device_hash: str
    lat: float | None
    lon: float | None
    gps_accuracy_m: float | None = None
    text: str | None = None
    language_hint: str | None = None
    photo: tuple[bytes, str] | None = None
    audio: tuple[bytes, str] | None = None
    source: str = "pwa"


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """What intake decided."""

    accepted: bool
    trace_id: str
    report_id: str | None = None
    ward_id: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    media_keys: list[str] = field(default_factory=list)


class IntakeAgent:
    """The gateway. Not a `Bus` consumer — it originates traces."""

    name = "A0"
    version = "1.0.0"

    def __init__(self, bus: Bus, *, persist: bool = True) -> None:
        self.bus = bus
        self.persist = persist

    @property
    def producer(self) -> Producer:
        return Producer(agent=self.name, version=self.version, model=None)

    # -- the entry point --------------------------------------------------

    def intake(self, submission: Submission) -> IntakeResult:
        """Validate, store and publish one submission."""
        trace_id = uuid7()

        with bind_trace(str(trace_id)):
            if self._rate_limited(submission.device_hash):
                return self._reject(
                    submission,
                    "rate_limited",
                    f"device exceeded {RATE_LIMIT} reports per "
                    f"{int(RATE_WINDOW.total_seconds() // 60)} minutes",
                )

            try:
                return self._accept(submission)
            except MediaRejected as rejection:
                return self._reject(submission, rejection.reason_code, str(rejection))

    # -- acceptance -------------------------------------------------------

    def _accept(self, submission: Submission) -> IntakeResult:
        media: list[StoredMedia] = []

        # Media first: a rejection here must happen before we mint a report.
        if submission.photo is not None:
            media.append(store_image(*submission.photo))
        if submission.audio is not None:
            media.append(store_audio(*submission.audio))

        location = self._resolve_location(submission, media)
        if location is None:
            return self._reject(
                submission,
                "missing_location",
                "submission carried no usable GPS and no geotagged photo",
            )

        lat, lon, accuracy = location

        if not inside_city(lat, lon):
            return self._reject(
                submission,
                "outside_city_boundary",
                f"location {lat:.5f},{lon:.5f} falls outside the city boundary",
            )

        if not submission.text and submission.photo is None and submission.audio is None:
            return self._reject(
                submission,
                "malformed_request",
                "submission carried neither text, photo nor audio",
            )

        ward = ward_for(lat, lon)
        report_id = uuid7()
        created_at = utcnow()

        payload: dict[str, Any] = {
            "report_id": str(report_id),
            "device_hash": submission.device_hash,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "location": {"lat": lat, "lon": lon},
            "gps_accuracy_m": accuracy,
            "ward_id": ward.ward_id if ward else None,
            "media_keys": [item.key for item in media],
            "raw_text": submission.text,
            "has_photo": submission.photo is not None,
            "has_audio": submission.audio is not None,
            "lang_hint": submission.language_hint,
            "source": submission.source,
        }

        envelope = Envelope.originate(
            topic="reports.ingested",
            correlation_id=str(report_id),
            producer=self.producer,
            # A0 makes no interpretive judgement; it either accepted a
            # well-formed submission or it did not.
            confidence=1.0,
            rationale=self._rationale(submission, ward.ward_id if ward else None, media),
            payload=payload,
        )

        self._persist_report(report_id, envelope.trace_id, payload, lat, lon)
        self._record_attempt(submission.device_hash, "accepted", None)
        self._emit(envelope)

        log.info(
            "report accepted",
            extra={"report_id": str(report_id), "ward_id": payload["ward_id"]},
        )

        return IntakeResult(
            accepted=True,
            trace_id=str(envelope.trace_id),
            report_id=str(report_id),
            ward_id=payload["ward_id"],
            media_keys=[item.key for item in media],
        )

    def _resolve_location(
        self, submission: Submission, media: list[StoredMedia]
    ) -> tuple[float, float, float] | None:
        """Device GPS, falling back to a photo's geotag.

        PRD section 7/A0 keeps the geotag through EXIF stripping precisely so it
        can corroborate or replace a missing device fix.
        """
        accuracy = submission.gps_accuracy_m
        has_fix = submission.lat is not None and submission.lon is not None
        usable = accuracy is None or accuracy <= MAX_USABLE_ACCURACY_M

        if has_fix and usable:
            assert submission.lat is not None and submission.lon is not None
            return submission.lat, submission.lon, accuracy if accuracy is not None else 0.0

        for item in media:
            if item.exif_location is not None:
                lat, lon = item.exif_location
                # A geotag carries no accuracy estimate; record it as unknown
                # rather than inventing a figure A2 would later weight by.
                log.info("falling back to photo geotag", extra={"key": item.key})
                return lat, lon, MAX_USABLE_ACCURACY_M

        return None

    @staticmethod
    def _rationale(submission: Submission, ward_id: str | None, media: list[StoredMedia]) -> str:
        modalities = [
            name
            for name, present in (
                ("text", bool(submission.text)),
                ("photo", submission.photo is not None),
                ("audio", submission.audio is not None),
            )
            if present
        ]
        where = f"ward {ward_id}" if ward_id else "no ward match"
        return (
            f"Accepted {submission.source} submission with {', '.join(modalities)}; "
            f"{len(media)} media object(s) stored; resolved to {where}."
        )

    # -- rejection --------------------------------------------------------

    def _reject(self, submission: Submission, reason_code: str, reason: str) -> IntakeResult:
        """Publish `reports.rejected`. A rejection is auditable too."""
        envelope = Envelope.originate(
            topic="reports.rejected",
            correlation_id=submission.device_hash,
            producer=self.producer,
            confidence=1.0,
            rationale=f"Rejected at intake: {reason}",
            payload={
                "report_id": None,
                "device_hash": submission.device_hash,
                "reason_code": reason_code,
                "reason": reason,
                "rejected_at": utcnow().isoformat().replace("+00:00", "Z"),
            },
        )

        outcome = "rate_limited" if reason_code == "rate_limited" else "rejected"
        self._record_attempt(submission.device_hash, outcome, reason_code)
        self._emit(envelope)

        log.info("report rejected", extra={"reason_code": reason_code})

        return IntakeResult(
            accepted=False,
            trace_id=str(envelope.trace_id),
            reason_code=reason_code,
            reason=reason,
        )

    # -- persistence ------------------------------------------------------

    def _emit(self, envelope: Envelope) -> None:
        """Archive then publish, the ordering every publisher uses."""
        if self.persist:
            archive(envelope)
        self.bus.publish(envelope.topic, envelope.to_dict())

    def _persist_report(
        self,
        report_id: Any,
        trace_id: Any,
        payload: dict[str, Any],
        lat: float,
        lon: float,
    ) -> None:
        """Insert the report row. A0 owns everything written here; the
        perception columns stay NULL until A1 fills them."""
        if not self.persist:
            return

        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reports (
                    report_id, trace_id, device_hash, created_at, geom,
                    gps_accuracy_m, ward_id, media_keys, raw_text, lang,
                    status, source
                ) VALUES (
                    %s, %s, %s, %s, ST_GeogFromText(%s),
                    %s, %s, %s, %s, %s, 'ingested', %s
                )
                ON CONFLICT (report_id) DO NOTHING
                """,
                (
                    str(report_id),
                    str(trace_id),
                    payload["device_hash"],
                    payload["created_at"],
                    Point(lat, lon).wkt(),
                    payload["gps_accuracy_m"],
                    payload["ward_id"],
                    payload["media_keys"],
                    payload["raw_text"],
                    payload["lang_hint"],
                    payload["source"],
                ),
            )

    # -- rate limiting ----------------------------------------------------

    def _rate_limited(self, device_hash: str) -> bool:
        if not self.persist:
            return False

        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM intake_attempts "
                "WHERE device_hash = %s AND created_at >= now() - %s",
                (device_hash, RATE_WINDOW),
            )
            row = cur.fetchone()

        if row is None:
            return False
        return int(row["n"]) >= RATE_LIMIT

    def _record_attempt(self, device_hash: str, outcome: str, reason_code: str | None) -> None:
        if not self.persist:
            return

        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO intake_attempts (attempt_id, device_hash, outcome, reason_code) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid7()), device_hash, outcome, reason_code),
            )
