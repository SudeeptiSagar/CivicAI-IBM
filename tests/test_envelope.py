"""The envelope model and its two propagation rules (PRD section 9.2)."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from agents.av_sentinel.layers.structural import verify_structural
from common.envelope import SCHEMA_VERSION, Envelope, Producer
from common.schemas import load_all
from tests.factories import payload_for

A0 = Producer(agent="A0", version="1.0.0")
A1 = Producer(agent="A1", version="1.2.0", model="granite-vision-3.2")


def _origin() -> Envelope:
    return Envelope.originate(
        topic="reports.ingested",
        correlation_id="report-1",
        producer=A0,
        confidence=0.99,
        rationale="Citizen submission accepted",
        payload=payload_for("reports.ingested"),
    )


# -- trace propagation ---------------------------------------------------


def test_originate_starts_a_new_trace() -> None:
    envelope = _origin()
    assert envelope.causation_id is None
    assert envelope.trace_id != envelope.message_id


def test_derive_propagates_trace_id_unchanged() -> None:
    """The single rule that makes any incident replayable to its source."""
    origin = _origin()
    child = origin.derive(
        topic="reports.understood",
        producer=A1,
        confidence=0.87,
        rationale="Pothole visible in image",
        payload=payload_for("reports.understood"),
    )
    assert child.trace_id == origin.trace_id


def test_derive_sets_causation_to_the_parent_message() -> None:
    origin = _origin()
    child = origin.derive(
        topic="reports.understood",
        producer=A1,
        confidence=0.87,
        rationale="Pothole visible in image",
        payload=payload_for("reports.understood"),
    )
    assert child.causation_id == origin.message_id
    assert child.message_id != origin.message_id


def test_trace_id_survives_a_long_chain() -> None:
    """A0 to A5 is five hops; the trace must still point home at the end."""
    envelope = _origin()
    hops = [
        ("reports.understood", A1),
        ("reports.linked", Producer(agent="A2", version="1.0.0")),
        ("incidents.updated", Producer(agent="A3", version="1.0.0")),
        ("incidents.prioritized", Producer(agent="A4", version="1.0.0")),
        ("incidents.routed", Producer(agent="A5", version="1.0.0")),
    ]
    previous = envelope
    for topic, producer in hops:
        current = previous.derive(
            topic=topic,
            producer=producer,
            confidence=0.8,
            rationale=f"handled by {producer.agent}",
            payload=payload_for(topic),
        )
        assert current.trace_id == envelope.trace_id
        assert current.causation_id == previous.message_id
        previous = current


def test_derive_keeps_correlation_id_by_default() -> None:
    origin = _origin()
    child = origin.derive(
        topic="reports.understood",
        producer=A1,
        confidence=0.8,
        rationale="extracted",
        payload=payload_for("reports.understood"),
    )
    assert child.correlation_id == origin.correlation_id


def test_derive_can_switch_correlation_id_when_the_subject_changes() -> None:
    """A3 turns a report_id into an incident_id; the trace still connects them."""
    origin = _origin()
    child = origin.derive(
        topic="incidents.updated",
        producer=Producer(agent="A3", version="1.0.0"),
        confidence=0.8,
        rationale="consolidated into incident",
        payload=payload_for("incidents.updated"),
        correlation_id="incident-42",
    )
    assert child.correlation_id == "incident-42"
    assert child.trace_id == origin.trace_id


# -- defaults and invariants --------------------------------------------


def test_verification_starts_pending() -> None:
    """Producers never pre-declare their own verdict."""
    envelope = _origin()
    assert envelope.verification.status == "pending"
    assert envelope.verification.verdict_id is None
    assert envelope.verification.checked_layers == []


def test_emitted_at_is_timezone_aware_utc() -> None:
    assert _origin().emitted_at.tzinfo == dt.UTC


def test_emitted_at_serialises_with_a_trailing_z() -> None:
    assert _origin().to_dict()["emitted_at"].endswith("Z")


def test_message_ids_are_unique() -> None:
    assert len({_origin().message_id for _ in range(200)}) == 200


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0])
def test_confidence_outside_the_unit_interval_is_refused(confidence: float) -> None:
    with pytest.raises(ValidationError):
        Envelope.originate(
            topic="reports.ingested",
            correlation_id="report-1",
            producer=A0,
            confidence=confidence,
            rationale="x",
            payload={},
        )


def test_empty_rationale_is_refused() -> None:
    """Every agent output carries a machine-readable rationale (PRD section 7)."""
    with pytest.raises(ValidationError):
        Envelope.originate(
            topic="reports.ingested",
            correlation_id="report-1",
            producer=A0,
            confidence=0.5,
            rationale="",
            payload={},
        )


def test_undeclared_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        Envelope(
            trace_id=_origin().trace_id,
            correlation_id="c",
            topic="reports.ingested",
            producer=A0,
            confidence=0.5,
            rationale="r",
            payload={},
            smuggled="value",  # type: ignore[call-arg]
        )


def test_non_semver_producer_version_is_refused() -> None:
    with pytest.raises(ValidationError):
        Producer(agent="A0", version="v1")


# -- round trip and drift ------------------------------------------------


def test_round_trip_through_the_wire_form() -> None:
    origin = _origin()
    restored = Envelope.from_dict(origin.to_dict())
    assert restored.to_dict() == origin.to_dict()


def test_a_model_built_envelope_passes_sentinel_l1() -> None:
    """The model and the JSON Schema have to agree, or agents emit messages
    that their own verifier rejects."""
    verdict = verify_structural(_origin().to_dict())
    assert verdict.passed, verdict.reasons


def test_model_fields_match_the_envelope_schema() -> None:
    """Guards against drift between common.envelope and schemas/envelope.v1.json."""
    schema_fields = set(load_all()["envelope.v1"]["properties"])
    model_fields = set(Envelope.model_fields)
    assert model_fields == schema_fields


def test_model_required_fields_match_the_schema() -> None:
    schema_required = set(load_all()["envelope.v1"]["required"])
    # Several fields are defaulted by the model, and causation_id is nullable,
    # but every one of them is always serialised - which is what "required"
    # asserts about the wire form.
    assert schema_required <= set(Envelope.model_fields)


def test_default_schema_version_is_the_current_contract() -> None:
    assert _origin().schema_version == SCHEMA_VERSION


# -- idempotency key -----------------------------------------------------


def test_idempotency_key_uses_the_prd_tuple() -> None:
    envelope = _origin()
    assert envelope.idempotency_key() == "|".join(
        (envelope.correlation_id, envelope.topic, envelope.schema_version, "1.0.0")
    )


def test_idempotency_key_is_stable_across_redeliveries() -> None:
    """Two envelopes for the same work must key identically even though their
    message_ids differ — that is what makes a redelivery a no-op."""
    first = _origin()
    second = Envelope.originate(
        topic=first.topic,
        correlation_id=first.correlation_id,
        producer=A0,
        confidence=0.4,
        rationale="a differently worded rationale",
        payload=payload_for("reports.ingested"),
    )
    assert first.idempotency_key() == second.idempotency_key()


def test_idempotency_key_separates_producer_versions() -> None:
    """A redeployed agent's output is new work, not a duplicate."""
    origin = _origin()
    upgraded = Envelope.originate(
        topic=origin.topic,
        correlation_id=origin.correlation_id,
        producer=Producer(agent="A0", version="2.0.0"),
        confidence=0.9,
        rationale="same report, new agent build",
        payload=payload_for("reports.ingested"),
    )
    assert origin.idempotency_key() != upgraded.idempotency_key()
