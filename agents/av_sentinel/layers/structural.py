"""Sentinel L1 — structural verification (PRD section 8.1).

Validates the envelope and payload against the JSON Schema registered for
`(topic, schema_version)`. Required fields, enums, ranges, confidence in [0,1],
non-null ids. A failure here is a hard reject: the event is quarantined and
downstream never sees it.

L1 is deliberately model-free and deterministic. It is the layer that still
works when every LLM in the system is unavailable or wrong, which is why the
PRD makes it the gate that cannot be skipped.

Sentinel cannot mutate business data (PRD section 8.2) — nothing here writes to
the envelope or to any business table. It returns a verdict; acting on that
verdict is the caller's job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from common.schemas import SchemaNotFoundError, validator_for

__all__ = ["LAYER", "Reason", "StructuralVerdict", "verify_structural"]

LAYER: Literal["L1"] = "L1"

# Failure codes. Stable strings: the trace viewer, quarantine triage and the
# drift metrics all group on these, so renaming one is a breaking change.
CODE_NOT_AN_OBJECT = "envelope_not_an_object"
CODE_MISSING_TOPIC = "missing_topic"
CODE_MISSING_SCHEMA_VERSION = "missing_schema_version"
CODE_UNKNOWN_CONTRACT = "unknown_contract"
CODE_SCHEMA_VIOLATION = "schema_violation"


#: Cap on a single failure message.
#:
#: jsonschema quotes the offending value in full, so a 768-float embedding of
#: the wrong length produces a multi-kilobyte string. Left uncapped that string
#: flows into the verdict envelope's rationale and blows the envelope's own
#: 2000-character limit — meaning Sentinel would crash on precisely the
#: malformed input it exists to catch. The head of the message carries the
#: diagnosis; the tail is noise.
MAX_REASON_LENGTH = 400


def _truncate(message: str, limit: int = MAX_REASON_LENGTH) -> str:
    if len(message) <= limit:
        return message
    return f"{message[: limit - 3]}..."


@dataclass(frozen=True, slots=True)
class Reason:
    """One specific thing that is wrong."""

    code: str
    message: str
    path: str | None = None

    def __post_init__(self) -> None:
        # frozen dataclass: assign through object.__setattr__.
        object.__setattr__(self, "message", _truncate(self.message))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass(frozen=True, slots=True)
class StructuralVerdict:
    """The outcome of L1 for one envelope."""

    verdict: Literal["pass", "fail_hard"]
    reasons: list[Reason] = field(default_factory=list)
    layer: Literal["L1"] = LAYER

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "layer": self.layer,
            "reasons": [r.to_dict() for r in self.reasons],
        }


def _fail(*reasons: Reason) -> StructuralVerdict:
    return StructuralVerdict(verdict="fail_hard", reasons=list(reasons))


def _format_path(path: Any) -> str:
    """Render a jsonschema error path as a dotted string, or "<root>"."""
    parts = [str(p) for p in path]
    return ".".join(parts) if parts else "<root>"


def verify_structural(envelope: Mapping[str, Any]) -> StructuralVerdict:
    """Run L1 against one envelope.

    Returns `pass` or `fail_hard` — L1 has no soft verdict, because a message
    that does not match its declared contract cannot be reasoned about at all.
    """
    if not isinstance(envelope, Mapping):
        return _fail(
            Reason(CODE_NOT_AN_OBJECT, f"envelope must be an object, got {type(envelope).__name__}")
        )

    topic = envelope.get("topic")
    if not isinstance(topic, str) or not topic:
        return _fail(Reason(CODE_MISSING_TOPIC, "topic is missing or not a string", "topic"))

    schema_version = envelope.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        return _fail(
            Reason(
                CODE_MISSING_SCHEMA_VERSION,
                "schema_version is missing or not a string",
                "schema_version",
            )
        )

    try:
        validator = validator_for(topic, schema_version)
    except SchemaNotFoundError as exc:
        # An unregistered contract is a hard reject, not a pass-through. This is
        # what stops a typo'd or rogue topic from reaching a consumer unchecked.
        return _fail(Reason(CODE_UNKNOWN_CONTRACT, str(exc), "topic"))

    reasons = [
        Reason(CODE_SCHEMA_VIOLATION, error.message, _format_path(error.absolute_path))
        for error in sorted(validator.iter_errors(dict(envelope)), key=lambda e: list(e.path))
    ]

    return StructuralVerdict(verdict="pass") if not reasons else _fail(*reasons)
