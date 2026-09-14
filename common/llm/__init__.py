"""Reasoning providers (PRD section 6.2).

`get_provider()` is the only way an agent obtains one. No agent imports a
vendor SDK, and no agent constructs a provider class directly, so the backing
model is a configuration decision.

    CIVICAI_LLM_PROVIDER=heuristic    deterministic lexical baseline, no model
    CIVICAI_LLM_PROVIDER=watsonx      IBM Granite (not implemented yet)
    CIVICAI_LLM_PROVIDER=             unset: every call fails loudly

Unset is the default and it is not an oversight. An agent that needs reasoning
and has no provider must fail to `deadletter` rather than emit something it did
not actually derive (PRD section 7).
"""

from __future__ import annotations

from functools import lru_cache

from common.config import settings
from common.llm.provider import (
    EMBEDDING_DIMENSIONS,
    CapabilityUnavailable,
    Extraction,
    LLMProvider,
    Transcript,
    VisionResult,
)

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "CapabilityUnavailable",
    "Extraction",
    "LLMProvider",
    "ProviderNotConfigured",
    "Transcript",
    "VisionResult",
    "get_provider",
]


class ProviderNotConfigured(RuntimeError):
    """No reasoning provider is configured."""

    def __init__(self) -> None:
        super().__init__(
            "no LLM provider configured: set CIVICAI_LLM_PROVIDER "
            "(heuristic | watsonx). Refusing to fabricate model output."
        )


@lru_cache(maxsize=4)
def get_provider(name: str | None = None) -> LLMProvider:
    """Build the configured provider.

    Args:
        name: override for `CIVICAI_LLM_PROVIDER`. Tests pass "heuristic".

    Raises:
        ProviderNotConfigured: when nothing is configured.
        ValueError: when the configured name is unknown.
    """
    chosen = name if name is not None else settings().llm_provider

    if not chosen:
        raise ProviderNotConfigured

    if chosen == "heuristic":
        from common.llm.heuristic import HeuristicProvider

        return HeuristicProvider()

    if chosen == "watsonx":
        # Deliberately not implemented rather than stubbed: a watsonx provider
        # that returned plausible output without calling watsonx would be the
        # exact failure this indirection exists to prevent.
        raise NotImplementedError(
            "the watsonx provider is not implemented yet - credentials are not "
            "available (PRD open question 2). Use CIVICAI_LLM_PROVIDER=heuristic "
            "for the deterministic lexical baseline."
        )

    raise ValueError(f"unknown LLM provider: {chosen!r}")
