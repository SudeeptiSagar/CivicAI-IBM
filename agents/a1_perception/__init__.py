"""A1 — Perception (PRD section 7/A1).

**Job:** "What is this person reporting?"
**In:** `reports.ingested`. **Out:** `reports.understood`.

Fuses whatever modalities are actually available into a structured extraction:
`category`, `subcategory`, `severity_raw`, `hazard_flags[]`, `landmark_text`,
`summary` and a 768-dimension `embedding`.

## Degrading honestly

The PRD describes ASR over audio and vision over images. Neither capability is
available in this build (no watsonx credentials — PRD open question 2), and the
lexical baseline behind `common.llm` offers neither.

A1 does not pretend otherwise. When a modality is present but has no provider
it records the gap in `unavailable_modalities`, lowers its confidence, and says
so in the rationale. What it never does is emit `vision_labels: []` as though
the photo had been examined and found empty — a downstream agent cannot tell
those two apart, and A2 would weight the result as if a photo had been read.

With text but no vision, A1 still produces a genuine extraction. With neither
text nor a usable modality, it emits a `*.skipped` event rather than guessing.

## Modality conflict

PRD section 7/A1 requires that when the image and the text disagree, A1 lowers
confidence and sets `modality_conflict = true` rather than picking one. That
rule is implemented and tested, but it cannot fire in this build: it needs
vision labels to disagree with. It is live the moment a vision-capable provider
is configured.
"""

from __future__ import annotations

from typing import Any

from agents.base import Agent, SkipSignal
from common.envelope import Envelope
from common.llm import CapabilityUnavailable, Extraction, get_provider
from common.llm.provider import EMBEDDING_DIMENSIONS, LLMProvider
from common.logging import get_logger

__all__ = ["GPS_REFINEMENT_THRESHOLD_M", "PerceptionAgent"]

log = get_logger(__name__)

#: PRD section 7/A1 refines location from landmark text when GPS accuracy is
#: worse than 50 m.
GPS_REFINEMENT_THRESHOLD_M = 50.0

#: How much a missing modality costs, per modality that was present but unread.
_MISSING_MODALITY_PENALTY = 0.15

#: PRD section 7/A1 caps the summary at 20 words.
_SUMMARY_WORD_LIMIT = 20


class PerceptionAgent(Agent):
    """Multimodal understanding, over whatever modalities are available."""

    name = "A1"
    version = "1.0.0"
    input_topic = "reports.ingested"

    def __init__(self, *args: Any, provider: LLMProvider | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Resolved once so a missing provider fails at construction, where it is
        # obvious, rather than on the first message.
        self._provider = provider if provider is not None else get_provider()
        self.model = self._provider.name

    def handle(self, envelope: Envelope) -> list[Envelope]:
        payload = envelope.payload

        transcript, unavailable = self._transcribe(payload)
        vision_labels, vision_gaps = self._vision(payload)
        unavailable.extend(vision_gaps)

        text = payload.get("raw_text")
        if not text and not transcript and not vision_labels:
            raise SkipSignal(
                "no_readable_modality",
                "Report carried nothing this build can read: "
                + (
                    f"{', '.join(unavailable)} present but unsupported"
                    if unavailable
                    else "no text, audio or image"
                ),
            )

        # Each modality is classified on its own before anything is fused.
        # Handing text and vision to one `extract()` call lets the provider
        # reconcile them internally, and a disagreement the PRD wants surfaced
        # disappears into whichever signal happened to match a keyword first.
        spoken = self._provider.extract(text=text, transcript=transcript)
        seen = (
            self._provider.extract(text=None, vision_labels=vision_labels)
            if vision_labels
            else None
        )

        conflict = self._modality_conflict(spoken, seen)

        # PRD section 7/A1: on conflict, do not pick one. The text is the
        # citizen's own account of what they are reporting, so it carries the
        # fields; the disagreement is recorded and the confidence halved rather
        # than resolved by fiat.
        extraction = spoken
        embedding = self._embedding(extraction, text, transcript)
        confidence = self._confidence(extraction, unavailable, conflict)

        understood: dict[str, Any] = {
            "report_id": payload["report_id"],
            "category": extraction.category,
            "subcategory": extraction.subcategory,
            "severity_raw": extraction.severity_raw,
            "hazard_flags": extraction.hazard_flags,
            "summary": _cap_summary(extraction.summary),
            "embedding": embedding,
            "transcript": transcript,
            "lang": payload.get("lang_hint"),
            "landmark_text": extraction.landmark_text,
            "location_refined": self._refined_location(payload, extraction),
            "vision_labels": vision_labels,
            "modality_conflict": conflict,
        }

        self._persist(understood)

        return [
            envelope.derive(
                topic="reports.understood",
                producer=self.producer,
                confidence=confidence,
                rationale=self._rationale(extraction, unavailable, conflict),
                payload=understood,
            )
        ]

    # -- modalities -------------------------------------------------------

    def _transcribe(self, payload: dict[str, Any]) -> tuple[str | None, list[str]]:
        """ASR, if the provider has it. Returns (transcript, gaps)."""
        if not payload.get("has_audio"):
            return None, []

        if "transcribe" not in self._provider.capabilities:
            log.info("audio present but ASR unavailable", extra={"provider": self._provider.name})
            return None, ["audio"]

        audio_key = _first_key(payload, ".audio")
        if audio_key is None:
            return None, ["audio"]

        from common.storage import fetch

        try:
            transcript = self._provider.transcribe(
                fetch(audio_key), language_hint=payload.get("lang_hint")
            )
        except CapabilityUnavailable:
            return None, ["audio"]
        return transcript.text, []

    def _vision(self, payload: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Vision, if the provider has it. Returns (labels, gaps)."""
        if not payload.get("has_photo"):
            return [], []

        if "describe_image" not in self._provider.capabilities:
            log.info(
                "photo present but vision unavailable",
                extra={"provider": self._provider.name},
            )
            return [], ["image"]

        image_key = _first_image_key(payload)
        if image_key is None:
            return [], ["image"]

        from common.storage import fetch

        try:
            result = self._provider.describe_image(fetch(image_key))
        except CapabilityUnavailable:
            return [], ["image"]
        return result.labels, []

    # -- derived fields ---------------------------------------------------

    def _embedding(
        self, extraction: Extraction, text: str | None, transcript: str | None
    ) -> list[float]:
        """Embed the text this extraction was actually derived from."""
        source = " ".join(part for part in (text, transcript, extraction.summary) if part)
        embedding = self._provider.embed(source)

        if len(embedding) != EMBEDDING_DIMENSIONS:
            # Sentinel L1 would reject this anyway; failing here names the
            # provider instead of blaming the message.
            raise ValueError(
                f"provider {self._provider.name!r} returned a "
                f"{len(embedding)}-dimension embedding, expected {EMBEDDING_DIMENSIONS}"
            )
        return embedding

    @staticmethod
    def _modality_conflict(spoken: Extraction, seen: Extraction | None) -> bool:
        """True when the image and the text describe different things.

        The PRD's example is a photo of garbage with text saying "streetlight".
        Both modalities have to have reached a definite view for that to count:
        `other` means "I could not tell", which is not disagreement, and
        treating it as such would flag every report whose photo the provider
        could not classify.

        Stays False whenever no vision provider is configured, because there is
        then nothing for the text to disagree with.
        """
        if seen is None:
            return False
        if spoken.category == "other" or seen.category == "other":
            return False
        return spoken.category != seen.category

    def _refined_location(
        self, payload: dict[str, Any], extraction: Extraction
    ) -> dict[str, float] | None:
        """PRD section 7/A1 refines location from landmark text above 50 m.

        Refinement needs a place lookup to turn "opposite St Francis School"
        into coordinates. No geocoding provider is configured, so A1 surfaces
        the landmark text for a human or a later agent and leaves the location
        alone rather than inventing a more precise fix than it has.
        """
        accuracy = payload.get("gps_accuracy_m")
        if accuracy is None or accuracy <= GPS_REFINEMENT_THRESHOLD_M:
            return None

        if extraction.landmark_text:
            log.info(
                "landmark available but no geocoder configured",
                extra={"landmark": extraction.landmark_text, "accuracy_m": accuracy},
            )
        return None

    @staticmethod
    def _confidence(extraction: Extraction, unavailable: list[str], conflict: bool) -> float:
        confidence = extraction.confidence
        confidence -= _MISSING_MODALITY_PENALTY * len(unavailable)
        if conflict:
            confidence *= 0.5
        return max(0.0, min(1.0, round(confidence, 4)))

    def _rationale(self, extraction: Extraction, unavailable: list[str], conflict: bool) -> str:
        parts = [extraction.rationale or f"Extracted category {extraction.category}."]
        if unavailable:
            parts.append(
                f"{', '.join(unavailable)} present but unread: provider "
                f"{self._provider.name!r} has no such capability; confidence lowered."
            )
        if conflict:
            parts.append("Image and text disagree; modality_conflict set.")
        return " ".join(parts)[:2000]

    # -- persistence ------------------------------------------------------

    def _persist(self, understood: dict[str, Any]) -> None:
        """Fill A1's columns on the report row. A0 owns the row; A1 owns these
        columns and writes nothing else."""
        if not self.persist:
            return

        from common.db import transaction

        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE reports
                SET category = %s,
                    subcategory = %s,
                    severity_raw = %s,
                    hazard_flags = %s,
                    summary = %s,
                    transcript = %s,
                    embedding = %s,
                    status = 'understood',
                    updated_at = now()
                WHERE report_id = %s
                """,
                (
                    understood["category"],
                    understood["subcategory"],
                    understood["severity_raw"],
                    understood["hazard_flags"],
                    understood["summary"],
                    understood["transcript"],
                    _vector_literal(understood["embedding"]),
                    understood["report_id"],
                ),
            )


def _cap_summary(summary: str) -> str:
    words = summary.split()
    return " ".join(words[:_SUMMARY_WORD_LIMIT])[:200]


def _vector_literal(embedding: list[float]) -> str:
    """pgvector's text input format."""
    return "[" + ",".join(f"{value:.6f}" for value in embedding) + "]"


def _first_image_key(payload: dict[str, Any]) -> str | None:
    for key in payload.get("media_keys", []):
        if key.endswith((".jpg", ".png", ".webp")):
            return str(key)
    return None


def _first_key(payload: dict[str, Any], suffix: str) -> str | None:
    for key in payload.get("media_keys", []):
        if key.endswith(suffix):
            return str(key)
    return None
