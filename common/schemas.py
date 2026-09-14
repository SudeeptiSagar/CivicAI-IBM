"""JSON Schema registry.

PRD section 11 makes `schemas/` the source of truth for message contracts, so
this module loads those files rather than deriving schemas from Python models.
`tests/test_envelope.py` asserts the Pydantic model in `common.envelope` stays
in step with `schemas/envelope.v1.json`.

Schemas are versioned by major version only: schema_version "1.4.2" resolves to
`<topic>.v1.json`, because payloads are additive-only within a major version
(PRD section 9.2).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from common.topics import schema_family_for

__all__ = [
    "SCHEMA_DIR",
    "SchemaNotFoundError",
    "iter_schema_paths",
    "load_all",
    "validator_for",
]

SCHEMA_DIR: Final = pathlib.Path(__file__).resolve().parent.parent / "schemas"


class SchemaNotFoundError(LookupError):
    """No schema is registered for the requested (topic, schema_version)."""


def iter_schema_paths() -> list[pathlib.Path]:
    """Every schema file on disk, sorted for deterministic iteration."""
    return sorted(SCHEMA_DIR.glob("*.json"))


@lru_cache(maxsize=1)
def load_all() -> dict[str, Any]:
    """Load every schema file, keyed by basename (e.g. "reports.ingested.v1")."""
    schemas: dict[str, Any] = {}
    for path in iter_schema_paths():
        schemas[path.name.removesuffix(".json")] = json.loads(path.read_text(encoding="utf-8"))
    return schemas


@lru_cache(maxsize=1)
def _registry() -> Registry[Any]:
    """A referencing Registry so cross-file $refs to the envelope resolve."""
    registry: Registry[Any] = Registry()
    for schema in load_all().values():
        registry = registry.with_resource(
            uri=schema["$id"], resource=Resource.from_contents(schema)
        )
    return registry


@lru_cache(maxsize=1)
def _format_checker() -> FormatChecker:
    """Format checker that actually enforces date-time.

    jsonschema treats unknown formats as annotations, so without this a
    malformed emitted_at would sail through L1. datetime.fromisoformat accepts
    the trailing "Z" on Python 3.11+, which is the form the envelope uses.
    """

    def is_rfc3339(value: object) -> bool:
        if not isinstance(value, str):
            return True  # type is the type keyword's job, not the format keyword's
        dt.datetime.fromisoformat(value)
        return True

    checker = FormatChecker()
    # Registered by call rather than as a decorator: jsonschema's `checks`
    # returns an untyped callable, which under strict mypy would erase the
    # annotations on the function it decorates.
    checker.checks("date-time", raises=ValueError)(is_rfc3339)
    return checker


@lru_cache(maxsize=256)
def validator_for(topic: str, schema_version: str) -> Draft202012Validator:
    """Return the validator for a (topic, schema_version) pair.

    Raises:
        SchemaNotFoundError: if the topic is unknown or no schema exists for
            that major version. Sentinel L1 turns this into a fail_hard rather
            than letting an unrecognised contract through.
    """
    family = schema_family_for(topic)
    if family is None:
        raise SchemaNotFoundError(f"unknown topic: {topic!r}")

    major = schema_version.split(".", 1)[0]
    key = f"{family}.v{major}"

    schema = load_all().get(key)
    if schema is None:
        raise SchemaNotFoundError(
            f"no schema {key!r} for topic {topic!r} at schema_version {schema_version!r}"
        )

    return Draft202012Validator(schema, registry=_registry(), format_checker=_format_checker())


def envelope_schema() -> Mapping[str, Any]:
    """The bare envelope schema, used by the model/schema drift test."""
    schema: Mapping[str, Any] = load_all()["envelope.v1"]
    return schema
