"""A1 invariants (PRD section 8.1).

PRD row: "Category ∈ taxonomy; summary ≤ 20 words; embedding dims = 768 and
non-zero; `modality_conflict` set whenever vision/text categories differ."

All four are self-contained — no database needed. The taxonomy mirrors
`schemas/envelope.v1.json#/$defs/category` exactly; `tests/test_invariants.py`
asserts the two stay in lockstep, the same drift-guard pattern
`db/migrations/0001_init.sql` uses for `departments`.

The dimension and non-zero checks are also enforced structurally by L1 for
the length (`schemas/reports.understood.v1.json` fixes `minItems`/`maxItems`
at 768), so this module's dimension check is a deliberate second line of
defence, cheap and harmless if L1 already caught it. **Non-zero is L2-only**:
nothing in the JSON Schema can express "not every component is exactly zero",
and an all-zero embedding is exactly what a provider returning a fabricated
default would look like — the "never invent what you could not read" rule
from ROADMAP's conventions, applied to Sentinel.

`modality_conflict` cannot be recomputed exactly: A1's own decision depends on
its vision provider's category guess, which is not carried on the wire, only
`vision_labels` (free-text labels) is. The check here is a heuristic keyword
match between `vision_labels` and `category`, so a mismatch is reported as
`warn`, not a hard failure — this is a plausibility re-check, not a
recomputation of A1's own logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from agents.av_sentinel.rules import Finding

__all__ = ["CATEGORY_TAXONOMY", "MAX_SUMMARY_WORDS", "check"]

#: Mirrors schemas/envelope.v1.json#/$defs/category.
CATEGORY_TAXONOMY: Final[frozenset[str]] = frozenset(
    {
        "pothole",
        "waterlogging",
        "drain_overflow",
        "sewage",
        "garbage",
        "streetlight",
        "traffic_obstruction",
        "road_damage",
        "water_supply",
        "stray_animals",
        "tree_hazard",
        "public_toilet",
        "encroachment",
        "other",
    }
)

MAX_SUMMARY_WORDS: Final = 20
EMBEDDING_DIMENSIONS: Final = 768

#: Loose keyword hints per category, for the modality_conflict plausibility
#: check only. Not a taxonomy of vision labels — just enough to catch the
#: PRD's own example (a photo of garbage, text says "streetlight").
_CATEGORY_KEYWORDS: Final[dict[str, frozenset[str]]] = {
    "pothole": frozenset({"pothole", "road", "crater", "cavity"}),
    "waterlogging": frozenset({"water", "flood", "standing_water"}),
    "drain_overflow": frozenset({"drain", "water", "overflow"}),
    "sewage": frozenset({"sewage", "drain", "smell"}),
    "garbage": frozenset({"garbage", "trash", "waste", "dump"}),
    "streetlight": frozenset({"streetlight", "lamp", "pole", "light"}),
    "traffic_obstruction": frozenset({"traffic", "obstruction", "vehicle", "block"}),
    "road_damage": frozenset({"road", "crack", "damage", "asphalt"}),
    "water_supply": frozenset({"pipe", "water", "leak", "tap"}),
    "stray_animals": frozenset({"dog", "cattle", "animal", "stray"}),
    "tree_hazard": frozenset({"tree", "branch", "fallen"}),
    "public_toilet": frozenset({"toilet", "sanitation"}),
    "encroachment": frozenset({"encroachment", "stall", "structure"}),
}


def check(topic: str, envelope: Mapping[str, Any], *, allow_db: bool) -> list[Finding]:
    if topic != "reports.understood":
        return []
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return []

    findings: list[Finding] = []
    findings.extend(_category_in_taxonomy(payload))
    findings.extend(_summary_word_count(payload))
    findings.extend(_embedding_shape(payload))
    findings.extend(_modality_conflict_plausible(payload))
    return findings


def _category_in_taxonomy(payload: Mapping[str, Any]) -> list[Finding]:
    category = payload.get("category")
    if category in CATEGORY_TAXONOMY:
        return []
    return [
        Finding(
            code="a1_category_outside_taxonomy",
            message=f"category {category!r} is not in the taxonomy",
            severity="fail_hard",
            path="payload.category",
        )
    ]


def _summary_word_count(payload: Mapping[str, Any]) -> list[Finding]:
    summary = payload.get("summary")
    if not isinstance(summary, str):
        return []
    words = len(summary.split())
    if words <= MAX_SUMMARY_WORDS:
        return []
    return [
        Finding(
            code="a1_summary_too_long",
            message=f"summary is {words} words, PRD 7/A1 caps it at {MAX_SUMMARY_WORDS}",
            severity="fail_soft",
            path="payload.summary",
        )
    ]


def _embedding_shape(payload: Mapping[str, Any]) -> list[Finding]:
    embedding = payload.get("embedding")
    if not isinstance(embedding, list):
        return []

    findings: list[Finding] = []
    if len(embedding) != EMBEDDING_DIMENSIONS:
        findings.append(
            Finding(
                code="a1_embedding_wrong_dimensions",
                message=(
                    f"embedding has {len(embedding)} dimensions, expected {EMBEDDING_DIMENSIONS}"
                ),
                severity="fail_hard",
                path="payload.embedding",
            )
        )
    if embedding and all(_is_zero(v) for v in embedding):
        findings.append(
            Finding(
                code="a1_embedding_all_zero",
                message="embedding is all zeros; looks like a fabricated default, not a real read",
                severity="fail_hard",
                path="payload.embedding",
            )
        )
    return findings


def _is_zero(value: Any) -> bool:
    return isinstance(value, int | float) and value == 0


def _modality_conflict_plausible(payload: Mapping[str, Any]) -> list[Finding]:
    if payload.get("modality_conflict") is not False:
        # Either already flagged True (nothing to re-check) or not a bool.
        return []
    category = payload.get("category")
    vision_labels = payload.get("vision_labels")
    if not isinstance(vision_labels, list) or not vision_labels:
        return []

    keywords = _CATEGORY_KEYWORDS.get(category if isinstance(category, str) else "")
    if not keywords:
        return []

    labels = {str(label).lower() for label in vision_labels}
    if labels & keywords:
        return []

    return [
        Finding(
            code="a1_modality_conflict_maybe_missed",
            message=(
                f"vision_labels {sorted(labels)} share no keyword with category "
                f"{category!r}, but modality_conflict is false"
            ),
            severity="warn",
            path="payload.modality_conflict",
        )
    ]
