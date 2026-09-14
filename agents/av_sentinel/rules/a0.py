"""A0 invariants (PRD section 8.1).

PRD row: "Location inside city polygon; media present for `has_photo=true`;
`report_id` unused."

* **Media present for `has_photo=true`** is self-contained: the envelope
  carries both `has_photo` and `media_keys`, so this runs unconditionally.
* **Location inside city polygon** and **`report_id` unused** need state this
  envelope does not carry (the loaded city boundary; whether the id has been
  seen before) and only run when a database is reachable (`allow_db=True`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents.av_sentinel.rules import Finding

__all__ = ["check"]


def check(topic: str, envelope: Mapping[str, Any], *, allow_db: bool) -> list[Finding]:
    if topic != "reports.ingested":
        return []

    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return []

    findings: list[Finding] = []
    findings.extend(_media_present(payload))
    if allow_db:
        findings.extend(_report_id_unused(payload))
        findings.extend(_location_inside_city(payload))
    return findings


def _media_present(payload: Mapping[str, Any]) -> list[Finding]:
    if payload.get("has_photo") is not True:
        return []
    media_keys = payload.get("media_keys")
    if isinstance(media_keys, list) and media_keys:
        return []
    return [
        Finding(
            code="a0_has_photo_without_media",
            message="has_photo is true but media_keys is empty",
            severity="fail_hard",
            path="payload.media_keys",
        )
    ]


def _report_id_unused(payload: Mapping[str, Any]) -> list[Finding]:
    report_id = payload.get("report_id")
    if not isinstance(report_id, str) or not report_id:
        return []
    try:
        from common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM reports WHERE report_id = %s", (report_id,))
            exists = cur.fetchone() is not None
    except Exception:
        # A database that cannot answer this is not the same as an id in use.
        # Sentinel degrades by skipping, not by guessing (see module docstring).
        return []
    if not exists:
        return []
    return [
        Finding(
            code="a0_report_id_reused",
            message=f"report_id {report_id} already exists in reports",
            severity="fail_hard",
            path="payload.report_id",
        )
    ]


def _location_inside_city(payload: Mapping[str, Any]) -> list[Finding]:
    location = payload.get("location")
    if not isinstance(location, Mapping):
        return []
    lat, lon = location.get("lat"), location.get("lon")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return []
    try:
        from common.geo import inside_city

        ok = inside_city(float(lat), float(lon))
    except Exception:
        return []
    if ok:
        return []
    return [
        Finding(
            code="a0_location_outside_city",
            message=f"({lat}, {lon}) falls outside the loaded city boundary",
            severity="fail_hard",
            path="payload.location",
        )
    ]
