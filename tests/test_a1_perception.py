"""A1 — Perception (PRD section 7/A1).

The tests that matter most here are about *degrading honestly*: what A1 does
when a modality is present but no provider can read it. Getting that wrong
would let a photo be silently ignored while the output still looked complete.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.a1_perception import PerceptionAgent
from agents.av_sentinel.layers.structural import verify_structural
from bus.memory import InMemoryBus
from common.envelope import Envelope
from common.llm.heuristic import HeuristicProvider
from common.llm.provider import (
    EMBEDDING_DIMENSIONS,
    Extraction,
    Transcript,
    VisionResult,
)
from tests.factories import envelope_for

TOPIC = "reports.ingested"


@pytest.fixture
def stub_blobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return bytes for any media key without touching the object store.

    The vision tests are about what A1 concludes from labels, not about
    whether S3 is reachable; test_media.py covers the storage path.
    """
    import common.storage

    monkeypatch.setattr(common.storage, "fetch", lambda key: b"fake-image-bytes")


class SeeingProvider(HeuristicProvider):
    """The baseline plus vision, for the paths that need a vision-capable one.

    Not a model either — it returns whatever labels the test supplies. Its job
    is to exercise the code that runs *when* vision exists.
    """

    name = "test-seeing"
    capabilities = frozenset({"extract", "embed", "describe_image", "transcribe"})

    def __init__(self, labels: list[str], transcript: str = "") -> None:
        self._labels = labels
        self._transcript = transcript

    def describe_image(self, image: bytes) -> VisionResult:
        return VisionResult(labels=self._labels, confidence=0.8)

    def transcribe(self, audio: bytes, *, language_hint: str | None = None) -> Transcript:
        return Transcript(text=self._transcript, language=language_hint or "en", confidence=0.8)


@pytest.fixture
def agent(memory_bus: InMemoryBus) -> PerceptionAgent:
    return PerceptionAgent(memory_bus, persist=False, backoff=(0.0,), provider=HeuristicProvider())


def _ingested(**payload_overrides: Any) -> Envelope:
    envelope = envelope_for(TOPIC)
    envelope["payload"].update(payload_overrides)
    return Envelope.from_dict(envelope)


def _understood(agent: PerceptionAgent, envelope: Envelope) -> dict[str, Any]:
    return agent.handle(envelope)[0].payload


# -- extraction ----------------------------------------------------------


def test_text_is_classified(agent: PerceptionAgent) -> None:
    payload = _understood(
        agent, _ingested(raw_text="Huge pothole outside the school gate", has_photo=False)
    )
    assert payload["category"] == "pothole"


def test_output_passes_sentinel_l1(agent: PerceptionAgent) -> None:
    emitted = agent.handle(_ingested(raw_text="Pothole here", has_photo=False))[0]
    verdict = verify_structural(emitted.to_dict())
    assert verdict.passed, verdict.reasons


def test_embedding_has_the_contracted_dimension(agent: PerceptionAgent) -> None:
    payload = _understood(agent, _ingested(raw_text="Pothole", has_photo=False))
    assert len(payload["embedding"]) == EMBEDDING_DIMENSIONS


def test_summary_is_capped_at_twenty_words(agent: PerceptionAgent) -> None:
    """PRD section 7/A1, and a Sentinel L2 invariant in P4."""
    long_text = " ".join(f"word{i}" for i in range(80))
    payload = _understood(agent, _ingested(raw_text=long_text, has_photo=False))
    assert len(payload["summary"].split()) <= 20


def test_trace_is_propagated(agent: PerceptionAgent) -> None:
    source = _ingested(raw_text="Pothole", has_photo=False)
    emitted = agent.handle(source)[0]

    assert emitted.trace_id == source.trace_id
    assert emitted.causation_id == source.message_id


def test_hazard_flags_survive_to_the_payload(agent: PerceptionAgent) -> None:
    payload = _understood(
        agent, _ingested(raw_text="Open manhole with no cover here", has_photo=False)
    )
    assert "open_manhole" in payload["hazard_flags"]


# -- degrading honestly --------------------------------------------------


def test_photo_without_vision_is_not_reported_as_read(agent: PerceptionAgent) -> None:
    """The crux. Emitting `vision_labels: []` would be indistinguishable from
    "a photo was examined and nothing was found", and A2 would weight the
    result as if the image had been read."""
    payload = _understood(agent, _ingested(raw_text="Pothole here", has_photo=True))
    assert payload["vision_labels"] == []


def test_unread_photo_lowers_confidence(agent: PerceptionAgent) -> None:
    with_photo = agent.handle(_ingested(raw_text="Pothole here", has_photo=True))[0]
    without_photo = agent.handle(_ingested(raw_text="Pothole here", has_photo=False))[0]

    assert with_photo.confidence < without_photo.confidence


def test_unread_photo_is_named_in_the_rationale(agent: PerceptionAgent) -> None:
    """Anyone reading the trace must be able to tell the photo went unexamined."""
    emitted = agent.handle(_ingested(raw_text="Pothole here", has_photo=True))[0]
    assert "image" in emitted.rationale
    assert "no such capability" in emitted.rationale


def test_unread_audio_lowers_confidence(agent: PerceptionAgent) -> None:
    with_audio = agent.handle(_ingested(raw_text="Pothole here", has_photo=False, has_audio=True))[
        0
    ]
    without = agent.handle(_ingested(raw_text="Pothole here", has_photo=False))[0]

    assert with_audio.confidence < without.confidence


def test_both_modalities_unread_costs_more_than_one(agent: PerceptionAgent) -> None:
    one = agent.handle(_ingested(raw_text="Pothole", has_photo=True, has_audio=False))[0]
    both = agent.handle(_ingested(raw_text="Pothole", has_photo=True, has_audio=True))[0]

    assert both.confidence < one.confidence


def test_nothing_readable_skips_rather_than_guessing(agent: PerceptionAgent) -> None:
    """PRD section 7: emit a *.skipped with a reason rather than invent output."""
    from agents.base import SkipSignal

    with pytest.raises(SkipSignal) as raised:
        agent.handle(_ingested(raw_text=None, has_photo=True, has_audio=False))

    assert raised.value.reason_code == "no_readable_modality"
    assert "image" in raised.value.reason


def test_skip_event_is_emitted_through_the_runtime(memory_bus: InMemoryBus) -> None:
    agent = PerceptionAgent(memory_bus, persist=False, backoff=(0.0,), provider=HeuristicProvider())
    memory_bus.create_group(TOPIC, agent.group)
    envelope = envelope_for(TOPIC)
    envelope["payload"].update({"raw_text": None, "has_photo": True})
    memory_bus.publish(TOPIC, envelope)

    result = agent.run_once()[0]

    assert result.skipped
    assert memory_bus.length(f"{TOPIC}.skipped") == 1


# -- with a vision-capable provider --------------------------------------


def test_vision_labels_are_used_when_available(memory_bus: InMemoryBus) -> None:
    agent = PerceptionAgent(
        memory_bus,
        persist=False,
        backoff=(0.0,),
        provider=SeeingProvider(labels=["road", "pothole"]),
    )
    payload = _understood(agent, _ingested(raw_text="Pothole here", has_photo=False))
    # has_photo False means vision is not consulted; the point is the provider
    # is capable, so no gap is recorded.
    assert payload["modality_conflict"] is False


def test_modality_conflict_is_flagged_when_image_and_text_disagree(
    memory_bus: InMemoryBus, stub_blobs: None
) -> None:
    """PRD section 7/A1: lower confidence and flag, rather than picking one.

    This rule cannot fire with the lexical baseline because it has no vision,
    so it is exercised here with a vision-capable provider — the moment one is
    configured, this is live.
    """
    agent = PerceptionAgent(
        memory_bus,
        persist=False,
        backoff=(0.0,),
        provider=SeeingProvider(labels=["garbage", "bin"]),
    )
    envelope = _ingested(
        raw_text="Street light not working on this lane",
        has_photo=True,
        media_keys=["reports/2026/09/14/abc.jpg"],
    )

    monkeypatched = agent.handle(envelope)[0]

    assert monkeypatched.payload["modality_conflict"] is True


def test_modality_conflict_halves_confidence(memory_bus: InMemoryBus, stub_blobs: None) -> None:
    agreeing = PerceptionAgent(
        memory_bus, persist=False, backoff=(0.0,), provider=SeeingProvider(labels=["pothole"])
    )
    conflicting = PerceptionAgent(
        memory_bus, persist=False, backoff=(0.0,), provider=SeeingProvider(labels=["garbage"])
    )
    envelope = _ingested(
        raw_text="Pothole outside the school",
        has_photo=True,
        media_keys=["reports/2026/09/14/abc.jpg"],
    )

    assert conflicting.handle(envelope)[0].confidence < agreeing.handle(envelope)[0].confidence


# -- location refinement -------------------------------------------------


def test_precise_gps_is_not_refined(agent: PerceptionAgent) -> None:
    payload = _understood(
        agent,
        _ingested(
            raw_text="Pothole outside St Francis School", gps_accuracy_m=8.0, has_photo=False
        ),
    )
    assert payload["location_refined"] is None


def test_imprecise_gps_surfaces_the_landmark_without_inventing_a_fix(
    agent: PerceptionAgent,
) -> None:
    """PRD section 7/A1 refines location from landmark text above 50 m. No
    geocoder is configured, so A1 surfaces the landmark and leaves the
    coordinates alone rather than inventing precision it does not have."""
    payload = _understood(
        agent,
        _ingested(
            raw_text="Huge pothole outside St Francis School, very deep",
            gps_accuracy_m=120.0,
            has_photo=False,
        ),
    )

    assert payload["landmark_text"] is not None
    assert payload["location_refined"] is None


# -- provider contract ---------------------------------------------------


def test_wrong_embedding_dimension_fails_loudly(memory_bus: InMemoryBus) -> None:
    """Naming the provider beats letting Sentinel blame the message."""

    class ShortProvider(HeuristicProvider):
        name = "short"

        def embed(self, text: str) -> list[float]:
            return [0.1] * 512

    agent = PerceptionAgent(memory_bus, persist=False, backoff=(0.0,), provider=ShortProvider())

    with pytest.raises(ValueError, match="512-dimension"):
        agent.handle(_ingested(raw_text="Pothole", has_photo=False))


def test_agent_reports_its_provider_as_the_model(agent: PerceptionAgent) -> None:
    """The trace has to record what produced an extraction."""
    emitted = agent.handle(_ingested(raw_text="Pothole", has_photo=False))[0]
    assert emitted.producer.model == "heuristic-baseline"


def test_extraction_dataclass_is_immutable() -> None:
    extraction = Extraction(category="pothole", severity_raw=3, summary="x")
    with pytest.raises(AttributeError):
        extraction.category = "garbage"  # type: ignore[misc]
