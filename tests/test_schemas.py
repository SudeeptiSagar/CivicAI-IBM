"""The schema registry and the contracts it serves.

PRD section 11 makes schemas/ the source of truth, so these tests guard the
files themselves: that they parse, that they are wired to the envelope, and
that every topic in the registry actually has one.
"""

from __future__ import annotations

import json

import pytest
from jsonschema.protocols import Validator

from common import schemas
from common.envelope import SCHEMA_VERSION
from common.topics import PRODUCERS, TOPICS
from tests.factories import ALL_TOPICS, envelope_for

ENVELOPE_ID = "https://civicai.dev/schemas/envelope.v1.json"


def test_every_schema_file_is_valid_json_schema() -> None:
    for path in schemas.iter_schema_paths():
        document = json.loads(path.read_text(encoding="utf-8"))
        Validator.check_schema(document)


def test_every_schema_declares_an_id_matching_its_filename() -> None:
    for path in schemas.iter_schema_paths():
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["$id"] == f"https://civicai.dev/schemas/{path.name}"


@pytest.mark.parametrize("topic", sorted(TOPICS))
def test_every_registered_topic_has_a_schema(topic: str) -> None:
    assert schemas.validator_for(topic, SCHEMA_VERSION) is not None


@pytest.mark.parametrize("topic", sorted(TOPICS))
def test_every_topic_schema_composes_the_envelope(topic: str) -> None:
    """A topic schema that forgot to $ref the envelope would validate a payload
    with no envelope at all, which is the whole contract gone."""
    document = schemas.load_all()[f"{topic}.v1"]
    assert {"$ref": ENVELOPE_ID} in document["allOf"]


@pytest.mark.parametrize("topic", sorted(TOPICS))
def test_every_topic_schema_pins_its_topic_constant(topic: str) -> None:
    document = schemas.load_all()[f"{topic}.v1"]
    assert document["properties"]["topic"] == {"const": topic}


def test_factories_cover_every_registered_topic() -> None:
    """A topic without a factory is a contract nothing has ever exercised."""
    assert set(ALL_TOPICS) >= TOPICS


def test_producers_match_the_schema_descriptions() -> None:
    """PRD section 9.3's producer column, kept honest against the schema files."""
    for topic, producer in PRODUCERS.items():
        description = schemas.load_all()[f"{topic}.v1"]["description"]
        assert description.endswith(f"Producer: {producer}.")


def test_schema_version_resolves_by_major_only() -> None:
    """Payloads are additive-only within a major version (PRD section 9.2), so
    1.0.0 and 1.7.3 must resolve to the same v1 schema."""
    assert schemas.validator_for("reports.ingested", "1.0.0").schema is (
        schemas.validator_for("reports.ingested", "1.7.3").schema
    )


def test_unknown_topic_raises() -> None:
    with pytest.raises(schemas.SchemaNotFoundError, match="unknown topic"):
        schemas.validator_for("reports.imaginary", SCHEMA_VERSION)


def test_unknown_major_version_raises() -> None:
    with pytest.raises(schemas.SchemaNotFoundError, match="no schema"):
        schemas.validator_for("reports.ingested", "9.0.0")


def test_date_time_format_is_actually_enforced() -> None:
    """jsonschema treats unknown formats as annotations by default; common.schemas
    registers a real date-time checker. Without it this envelope would pass."""
    validator = schemas.validator_for("reports.ingested", SCHEMA_VERSION)
    envelope = envelope_for("reports.ingested", emitted_at="not-a-timestamp")
    errors = list(validator.iter_errors(envelope))
    assert any("not-a-timestamp" in error.message for error in errors)
