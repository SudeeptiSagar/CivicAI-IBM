"""The reasoning provider interface and the lexical baseline.

The baseline is not a model, and several of these tests exist specifically to
pin that down: it must be deterministic, it must refuse the capabilities it
does not have, and it must not report high confidence.
"""

from __future__ import annotations

import pytest

from common.llm import (
    CapabilityUnavailable,
    ProviderNotConfigured,
    get_provider,
)
from common.llm.heuristic import HeuristicProvider
from common.llm.provider import EMBEDDING_DIMENSIONS, LLMProvider


@pytest.fixture
def provider() -> HeuristicProvider:
    return HeuristicProvider()


# -- the protocol --------------------------------------------------------


def test_baseline_satisfies_the_protocol(provider: HeuristicProvider) -> None:
    assert isinstance(provider, LLMProvider)


def test_factory_builds_the_baseline() -> None:
    assert get_provider("heuristic").name == "heuristic-baseline"


def test_unconfigured_provider_fails_loudly() -> None:
    """PRD section 7: fail loudly rather than guess. An agent with no provider
    must not quietly fall back to anything."""
    with pytest.raises(ProviderNotConfigured):
        get_provider("")


def test_watsonx_is_not_stubbed() -> None:
    """A watsonx provider that returned plausible output without calling
    watsonx is the exact failure this indirection exists to prevent."""
    with pytest.raises(NotImplementedError, match="not implemented"):
        get_provider("watsonx")


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown LLM provider"):
        get_provider("clairvoyance")


# -- capabilities it does not have ---------------------------------------


def test_baseline_declares_only_what_it_has(provider: HeuristicProvider) -> None:
    assert provider.capabilities == {"extract", "embed"}


def test_transcribe_refuses(provider: HeuristicProvider) -> None:
    """No ASR. Returning an empty transcript would be indistinguishable from
    silence, and A1 would treat it as a read modality."""
    with pytest.raises(CapabilityUnavailable, match="transcribe"):
        provider.transcribe(b"audio")


def test_describe_image_refuses(provider: HeuristicProvider) -> None:
    with pytest.raises(CapabilityUnavailable, match="describe_image"):
        provider.describe_image(b"image")


# -- extraction ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Huge pothole outside the school gate", "pothole"),
        ("Knee deep waterlogging at the junction", "waterlogging"),
        ("Garbage dump near the market", "garbage"),
        ("Street light not working on this lane", "streetlight"),
        ("Blocked drain overflowing onto the road", "drain_overflow"),
        ("Sewage smell coming from the manhole", "sewage"),
        ("Tree branch fell on the footpath", "tree_hazard"),
        ("Pipeline burst, water leaking", "water_supply"),
        ("Stray dog menace near the park", "stray_animals"),
    ],
)
def test_category_keywords(provider: HeuristicProvider, text: str, expected: str) -> None:
    assert provider.extract(text=text).category == expected


def test_unmatched_text_falls_through_to_other(provider: HeuristicProvider) -> None:
    """Honest failure: no keyword matched, so it says so rather than guessing
    the nearest category."""
    result = provider.extract(text="The situation here is quite unsatisfactory")
    assert result.category == "other"
    assert result.confidence < 0.3


def test_kannada_text_is_not_silently_misclassified(provider: HeuristicProvider) -> None:
    """PRD section 5 expects Kannada submissions and the baseline cannot read
    them. It must land in 'other' with low confidence, not be forced into a
    plausible-looking category (PRD open question 3)."""
    result = provider.extract(text="ಇಲ್ಲಿ ದೊಡ್ಡ ಗುಂಡಿ ಇದೆ")
    assert result.category == "other"
    assert result.confidence < 0.3


def test_confidence_never_exceeds_the_baseline_cap(provider: HeuristicProvider) -> None:
    """No keyword matcher deserves to look confident."""
    result = provider.extract(text="pothole pothole pothole deep dangerous school")
    assert result.confidence <= 0.55


def test_hazard_flags_are_detected(provider: HeuristicProvider) -> None:
    result = provider.extract(text="Open manhole with no cover, someone will fall in")
    assert "open_manhole" in result.hazard_flags


def test_hazard_flags_stay_narrow(provider: HeuristicProvider) -> None:
    """These drive A4's life-safety floor of 85; a false positive would push a
    minor report to critical."""
    result = provider.extract(text="Garbage piling up near the market")
    assert result.hazard_flags == []


def test_severity_escalates_on_vulnerable_context(provider: HeuristicProvider) -> None:
    plain = provider.extract(text="Pothole on the road")
    near_school = provider.extract(text="Pothole on the road, a child nearly fell in")
    assert near_school.severity_raw > plain.severity_raw


def test_severity_stays_in_range(provider: HeuristicProvider) -> None:
    extreme = provider.extract(
        text="child hospital accident injury death elderly ambulance huge massive dangerous deep"
    )
    assert 1 <= extreme.severity_raw <= 5


def test_summary_is_capped_at_twenty_words(provider: HeuristicProvider) -> None:
    """PRD section 7/A1 caps the summary at 20 words."""
    result = provider.extract(text=" ".join(f"word{i}" for i in range(60)))
    assert len(result.summary.split()) <= 20


def test_landmark_is_extracted(provider: HeuristicProvider) -> None:
    result = provider.extract(text="Huge pothole outside St Francis School, very deep")
    assert result.landmark_text is not None
    assert "francis" in result.landmark_text.lower()


def test_extraction_with_no_input_is_explicit(provider: HeuristicProvider) -> None:
    result = provider.extract(text=None)
    assert result.confidence == 0.0
    assert result.category == "other"


def test_rationale_says_it_is_not_a_model(provider: HeuristicProvider) -> None:
    """Anyone reading a trace must be able to tell what produced this."""
    result = provider.extract(text="Pothole outside the school")
    assert "not a model" in result.rationale.lower()


# -- determinism ---------------------------------------------------------


def test_extraction_is_deterministic(provider: HeuristicProvider) -> None:
    text = "Deep pothole near the school gate, autos swerving"
    assert provider.extract(text=text) == provider.extract(text=text)


def test_embedding_is_deterministic(provider: HeuristicProvider) -> None:
    text = "Waterlogging at the junction"
    assert provider.embed(text) == provider.embed(text)


# -- embedding -----------------------------------------------------------


def test_embedding_has_the_contracted_dimension(provider: HeuristicProvider) -> None:
    """PRD section 10 fixes this at 768 and Sentinel L1 enforces it."""
    assert len(provider.embed("pothole")) == EMBEDDING_DIMENSIONS


def test_embedding_is_unit_length(provider: HeuristicProvider) -> None:
    """Normalised so cosine similarity is a plain dot product, and so pgvector's
    <=> operator means what A2 assumes it means."""
    magnitude = sum(value * value for value in provider.embed("pothole near school")) ** 0.5
    assert magnitude == pytest.approx(1.0, abs=1e-9)


def test_empty_text_embeds_to_zero(provider: HeuristicProvider) -> None:
    assert provider.embed("") == [0.0] * EMBEDDING_DIMENSIONS


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_shared_wording_scores_higher_than_unrelated(provider: HeuristicProvider) -> None:
    pothole_a = provider.embed("Huge pothole outside the school gate")
    pothole_b = provider.embed("Deep pothole near the school gate")
    garbage = provider.embed("Garbage dump near the market has not been cleared")

    assert _cosine(pothole_a, pothole_b) > _cosine(pothole_a, garbage)


def test_lexical_limits_are_real(provider: HeuristicProvider) -> None:
    """The honest ceiling of a bag-of-n-grams: two descriptions of the same
    pothole that share almost no substrings score poorly. A real embedding
    would place these together, which is why dedup recall should be expected to
    improve materially with a model behind the interface."""
    plain = provider.embed("There is a pothole here")
    paraphrase = provider.embed("The road surface has collapsed into a crater")

    assert _cosine(plain, paraphrase) < 0.5
