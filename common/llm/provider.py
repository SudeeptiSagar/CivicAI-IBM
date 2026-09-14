"""The reasoning provider interface (PRD section 6.2).

**No agent imports a vendor SDK directly.** Everything an agent needs from a
model goes through the protocol below, so swapping watsonx for Anthropic, or
for a local model, is a config change rather than an edit to eight agents.

Four capabilities, deliberately separate, because a provider may have some and
not others:

    extract          text (plus any vision labels and transcript) -> structured
    embed            text -> a 768-dimension vector
    transcribe       audio -> text            (ASR)
    describe_image   image -> labels          (vision)

A provider that lacks a capability raises `CapabilityUnavailable` rather than
returning something plausible. That is the whole point: PRD section 7 says an
agent fails loudly rather than guessing, and a provider that quietly returned
`[]` for vision would let A1 emit an extraction that silently ignored a photo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "CapabilityUnavailable",
    "Extraction",
    "LLMProvider",
    "Transcript",
    "VisionResult",
]

#: Fixed by PRD section 10's `embedding VECTOR(768)` and the reports.understood
#: schema. A provider whose native dimension differs must project to this.
EMBEDDING_DIMENSIONS = 768


class CapabilityUnavailable(RuntimeError):
    """The configured provider cannot do this.

    Raised rather than returning an empty or default result, so the caller has
    to decide what an honest output looks like without it.
    """

    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(
            f"provider {provider!r} has no {capability!r} capability; "
            f"configure one that does, or handle the gap explicitly"
        )
        self.provider = provider
        self.capability = capability


@dataclass(frozen=True, slots=True)
class Extraction:
    """What a citizen is reporting, as structured fields (PRD section 7/A1)."""

    category: str
    severity_raw: int
    summary: str
    hazard_flags: list[str] = field(default_factory=list)
    subcategory: str | None = None
    landmark_text: str | None = None
    #: How much the provider trusts this extraction, in [0,1].
    confidence: float = 0.5
    #: Machine-readable account of how the fields were reached.
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class Transcript:
    """ASR output."""

    text: str
    language: str
    confidence: float = 0.5


@dataclass(frozen=True, slots=True)
class VisionResult:
    """Vision output over one image."""

    labels: list[str]
    #: Free-text scene description, when the provider offers one.
    description: str | None = None
    confidence: float = 0.5


@runtime_checkable
class LLMProvider(Protocol):
    """What every reasoning backend must offer.

    `capabilities` lets a caller check before asking, so an agent can degrade
    deliberately instead of catching an exception as control flow.
    """

    name: str
    capabilities: frozenset[str]

    def extract(
        self,
        *,
        text: str | None,
        transcript: str | None = None,
        vision_labels: list[str] | None = None,
    ) -> Extraction:
        """Fuse the available modalities into a structured extraction."""
        ...

    def embed(self, text: str) -> list[float]:
        """Return a 768-dimension vector for `text`."""
        ...

    def transcribe(self, audio: bytes, *, language_hint: str | None = None) -> Transcript:
        """Transcribe audio. Raises CapabilityUnavailable if unsupported."""
        ...

    def describe_image(self, image: bytes) -> VisionResult:
        """Describe an image. Raises CapabilityUnavailable if unsupported."""
        ...
