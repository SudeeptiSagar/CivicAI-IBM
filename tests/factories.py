"""Valid example messages for every topic.

These double as executable documentation of the contracts and as proof that
each schema is actually satisfiable — a schema nothing can satisfy would
otherwise sit in the repo looking fine until an agent tried to emit against it.

Keep these minimal-but-valid: every required field present, nothing more,
unless the optional field is what a test is about.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from common.envelope import SCHEMA_VERSION, Envelope, Producer
from common.ids import uuid7
from common.topics import PRODUCERS

__all__ = ["ALL_TOPICS", "envelope_for", "payload_for", "ts", "uid"]

BENGALURU = {"lat": 12.9345, "lon": 77.6101}


def ts(offset_seconds: int = 0) -> str:
    """An RFC 3339 timestamp with a trailing Z."""
    moment = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=offset_seconds)
    return moment.isoformat().replace("+00:00", "Z")


def uid() -> str:
    """A fresh UUIDv7 as a string."""
    return str(uuid7())


def _scores() -> dict[str, float]:
    return {"spatial": 0.91, "temporal": 0.88, "semantic": 0.79, "visual": 0.65}


def _factor_breakdown() -> list[dict[str, Any]]:
    """All six factors from PRD section 7/A4, weights summing to 1.0."""
    factors = [
        ("hazard_severity", 0.30, 0.80),
        ("exposure", 0.20, 0.60),
        ("vulnerable_site_proximity", 0.15, 1.00),
        ("corroboration", 0.15, 0.55),
        ("age_unresolved", 0.10, 0.30),
        ("velocity", 0.10, 0.45),
    ]
    return [
        {
            "factor": name,
            "weight": weight,
            "value": value,
            "contribution": round(weight * value * 100, 4),
        }
        for name, weight, value in factors
    ]


_PAYLOADS: dict[str, Callable[[], dict[str, Any]]] = {
    "reports.ingested": lambda: {
        "report_id": uid(),
        "device_hash": "sha256:9f2c1ab4",
        "created_at": ts(),
        "location": dict(BENGALURU),
        "gps_accuracy_m": 8.5,
        "ward_id": "BLR-151",
        "media_keys": ["s3://civicai-media/2026/09/abc.jpg"],
        "raw_text": "Huge pothole outside the school gate",
        "has_photo": True,
        "has_audio": False,
        "lang_hint": "en",
        "source": "pwa",
    },
    "reports.rejected": lambda: {
        "report_id": None,
        "device_hash": "sha256:9f2c1ab4",
        "reason_code": "outside_city_boundary",
        "reason": "Location 13.9821,77.1044 falls outside the Bengaluru boundary",
        "rejected_at": ts(),
    },
    "reports.understood": lambda: {
        "report_id": uid(),
        "category": "pothole",
        "subcategory": "deep_cavity",
        "severity_raw": 4,
        "hazard_flags": ["traffic_hazard"],
        "summary": "Deep pothole at school gate, water-filled, cars swerving",
        "embedding": [0.01] * 768,
        "transcript": "Here there is a very big pothole near the school",
        "lang": "en",
        "landmark_text": "opposite St Francis School gate",
        "location_refined": dict(BENGALURU),
        "vision_labels": ["road", "pothole", "standing_water"],
        "modality_conflict": False,
    },
    "reports.linked": lambda: {
        "report_id": uid(),
        "decision": "auto_link",
        "incident_id": uid(),
        "match_score": 0.87,
        "component_scores": _scores(),
        "candidates": [{"incident_id": uid(), "score": 0.87, "component_scores": _scores()}],
        "radius_m": 75.0,
        "window_days": 21.0,
    },
    "incidents.updated": lambda: {
        "incident_id": uid(),
        "mode": "consolidation",
        "title": "Pothole cluster at St Francis School gate",
        "category": "pothole",
        "centroid": dict(BENGALURU),
        "ward_id": "BLR-151",
        "first_reported_at": ts(-86400),
        "last_reported_at": ts(),
        "report_count": 4,
        "distinct_reporters": 3,
        "member_report_ids": [uid(), uid(), uid(), uid()],
        "status": "open",
        "super_incident": None,
    },
    "incidents.prioritized": lambda: {
        "incident_id": uid(),
        "priority_score": 93.0,
        "priority_band": "critical",
        "factor_breakdown": _factor_breakdown(),
        "why": "Deep pothole 40 m from a school gate, 3 distinct reporters in 24 h.",
        "life_safety_floor_applied": False,
    },
    "incidents.routed": lambda: {
        "incident_id": uid(),
        "primary_department": "roads_and_infrastructure",
        "cc_departments": ["drainage_and_water"],
        "ward_id": "BLR-151",
        "office_id": "BLR-151-ROADS",
        "sla_due_at": ts(86400),
        "sla_hours": 24.0,
        "priority_band": "critical",
    },
    "incidents.unrouted": lambda: {
        "incident_id": uid(),
        "attempted_category": "other",
        "reason_code": "unknown_category",
        "reason": "Category 'other' has no department mapping; needs human triage",
    },
    "evidence.attached": lambda: {
        "evidence_id": uid(),
        "incident_id": uid(),
        "kind": "cctv_sample",
        "verdict": "corroborating",
        "confidence": 0.72,
        "source_uri": "file://data/sample_cctv/junction_12/frame_0042.jpg",
        "sampled_at": ts(-3600),
        "notes": "Standing water visible in 3 of 5 sampled frames",
    },
    "resolution.claimed": lambda: {
        "resolution_id": uid(),
        "incident_id": uid(),
        "claimed_by": "officer:BLR-151-ROADS:4471",
        "claimed_at": ts(),
        "note": "Patched and compacted",
        "claim_media_keys": ["s3://civicai-media/2026/09/after-abc.jpg"],
    },
    "resolution.verified": lambda: {
        "resolution_id": uid(),
        "incident_id": uid(),
        "final_status": "verified",
        "citizen_confirmations": 2,
        "citizen_disputes": 0,
        "visual_verdict": "fixed",
        "verified_at": ts(),
    },
    "resolution.disputed": lambda: {
        "resolution_id": uid(),
        "incident_id": uid(),
        "final_status": "disputed",
        "citizen_confirmations": 0,
        "citizen_disputes": 2,
        "visual_verdict": "unchanged",
        "reopened": True,
        "disputed_at": ts(),
        "escalated_priority_band": "critical",
    },
    "verification.results": lambda: {
        "verdict_id": uid(),
        "message_id": uid(),
        "trace_id": uid(),
        "topic": "reports.understood",
        "agent": "A1",
        "layer": "L1",
        "verdict": "pass",
        "reasons": [],
        "judge_model": None,
        "created_at": ts(),
    },
    "sentinel.alert": lambda: {
        "alert_id": uid(),
        "kind": "drift",
        "severity": "warning",
        "detail": "Mean A1 confidence fell 2.4 sigma below the trailing baseline",
        "created_at": ts(),
        "metric": "a1.mean_confidence",
        "observed": 0.61,
        "expected": 0.84,
        "sigma": -2.4,
        "subject_id": "A1",
    },
    "deadletter": lambda: {
        "agent": "A2",
        "attempts": 3,
        "error_chain": ["TimeoutError: pgvector query exceeded 5s"] * 3,
        "original_envelope": {},
        "failed_at": ts(),
    },
    # Patterned families (PRD sections 7, 8.2, 9.3).
    "reports.understood.skipped": lambda: {
        "input_message_id": uid(),
        "reason_code": "no_media_and_no_text",
        "reason": "Report carried neither media nor text; nothing to extract",
    },
    "quarantine.reports.understood": lambda: {
        "original_envelope": {},
        "verdict_id": uid(),
        "quarantined_at": ts(),
        "reasons": [
            {
                "code": "schema_violation",
                "message": "embedding has 512 items",
                "path": "payload.embedding",
            }
        ],
    },
    "control.a2_dedup": lambda: {
        "command": "pause",
        "params": {"reason": "index rebuild"},
    },
}

#: Every topic with a factory: the 15 concrete topics plus one member of each
#: patterned family.
ALL_TOPICS: tuple[str, ...] = tuple(_PAYLOADS)


def payload_for(topic: str) -> dict[str, Any]:
    """A valid payload for `topic`."""
    try:
        return _PAYLOADS[topic]()
    except KeyError:
        raise KeyError(f"no factory for topic {topic!r}") from None


def envelope_for(topic: str, **overrides: Any) -> dict[str, Any]:
    """A valid envelope dict for `topic`, with optional top-level overrides."""
    agent = PRODUCERS.get(topic, "AV")
    envelope = Envelope(
        trace_id=uuid7(),
        causation_id=uuid7(),
        correlation_id=uid(),
        topic=topic,
        schema_version=SCHEMA_VERSION,
        producer=Producer(agent=agent, version="1.0.0", model=None),
        confidence=0.9,
        rationale=f"factory-built example for {topic}",
        payload=payload_for(topic),
    ).to_dict()
    envelope.update(overrides)
    return envelope
