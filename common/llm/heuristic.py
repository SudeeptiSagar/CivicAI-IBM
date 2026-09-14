"""A deterministic lexical baseline. **This is not a model.**

Read this before using it, because mistaking it for perception would be the
single most misleading thing in the codebase.

## What it is

Keyword matching for category and hazard flags, small rules for severity, and a
hashed character-n-gram vector for `embed()`. Every part is deterministic: the
same text always produces the same output, with no network call and no weights.

## Why it exists

PRD section 6.2 puts IBM watsonx behind `llm/provider.py`, and no watsonx
credentials are configured yet. Without *something* implementing the protocol,
A1 cannot run, so A2 and A3 have no input, so M1 ("20 seeded reports collapse
into the expected incidents") cannot be demonstrated at all — the entire core
path would sit untested behind a missing API key.

This baseline keeps the pipeline runnable and, more usefully, gives the eval
harness a floor to measure the real provider against. A dedup rate that a
keyword matcher already achieves is not evidence that a model is working.

## What it is emphatically not

* **Not semantic.** `embed()` is lexical. "Road has a crater" and "pothole
  here" share no n-grams and will score near zero, where a real embedding would
  place them together. Dedup recall (PRD G1, ≥ 0.80) should be expected to
  *improve* materially with a real provider.
* **Not multimodal.** `transcribe()` and `describe_image()` raise
  `CapabilityUnavailable`. There is no ASR and no vision here, and A1 degrades
  openly rather than pretending otherwise.
* **Not multilingual.** Keywords are English. PRD section 5 expects Kannada and
  Hindi reports; those will fall through to `other` with low confidence, which
  is the honest outcome and is exactly what PRD open question 3 is about.

Confidence is reported low on purpose (see `_BASELINE_CONFIDENCE_CAP`) so that
downstream agents and Sentinel treat these extractions as weak evidence.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Final

from common.llm.provider import (
    EMBEDDING_DIMENSIONS,
    CapabilityUnavailable,
    Extraction,
    Transcript,
    VisionResult,
)

__all__ = ["HeuristicProvider"]

#: No keyword matcher deserves more trust than this, whatever it matched.
#: A real provider reports its own calibrated confidence instead.
_BASELINE_CONFIDENCE_CAP: Final = 0.55

#: Category keywords, most specific first — `drain_overflow` must win over
#: `sewage` when a report says "drain overflowing with sewage", because the
#: overflow is the reportable event and the sewage is its content.
_CATEGORY_KEYWORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("open_manhole_marker", ("manhole", "man hole", "manhole cover")),
    (
        "drain_overflow",
        (
            "drain overflow",
            "drain is overflow",
            "overflowing drain",
            "storm drain",
            "drain block",
            "blocked drain",
            "choked drain",
        ),
    ),
    ("sewage", ("sewage", "sewer", "septic", "foul smell", "stinking water")),
    (
        "waterlogging",
        (
            "waterlog",
            "water log",
            "flood",
            "standing water",
            "knee deep",
            "water accumulat",
            "submerged",
        ),
    ),
    ("pothole", ("pothole", "pot hole", "crater", "road hole", "hole in the road")),
    (
        "road_damage",
        (
            "road damage",
            "broken road",
            "cracked road",
            "road caved",
            "road collapse",
            "uneven road",
            "bad road",
        ),
    ),
    ("garbage", ("garbage", "trash", "rubbish", "waste", "dump", "litter")),
    (
        "streetlight",
        (
            "streetlight",
            "street light",
            "street lamp",
            "lamp post",
            "light not working",
            "dark street",
        ),
    ),
    ("water_supply", ("water supply", "no water", "pipeline burst", "water leak", "burst pipe")),
    ("tree_hazard", ("tree fallen", "fallen tree", "branch fell", "tree branch", "uprooted tree")),
    ("stray_animals", ("stray dog", "stray cattle", "stray animal", "dog menace")),
    (
        "traffic_obstruction",
        ("traffic jam", "road blocked", "blocking the road", "illegal parking", "obstruct"),
    ),
    ("public_toilet", ("public toilet", "toilet", "urinal")),
    ("encroachment", ("encroach", "footpath occupied", "illegal construction")),
)

#: Hazard keywords. These drive A4's life-safety floor, so they are kept narrow
#: — a false positive here would push a minor report to priority 85.
_HAZARD_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "open_manhole": ("open manhole", "manhole open", "uncovered manhole", "missing cover"),
    "live_wire": ("live wire", "exposed wire", "electric wire", "sparking", "shock"),
    "collapsed_structure": ("collapsed", "building fell", "wall fell", "caved in"),
    "gas_leak": ("gas leak", "smell of gas", "lpg leak"),
    "deep_water": ("knee deep", "waist deep", "chest deep", "submerged", "deep water"),
    "sharp_debris": ("broken glass", "sharp", "nails", "debris"),
    "fire_risk": ("fire", "burning", "smoke"),
    "blocked_emergency_access": ("ambulance", "fire engine", "emergency access"),
    "structural_crack": ("crack in the wall", "structural crack", "building crack"),
    "traffic_hazard": ("accident", "vehicles swerv", "bike skid", "collision", "near miss"),
}

#: Words that argue the problem is worse than its category's baseline.
_SEVERITY_ESCALATORS: Final[tuple[tuple[str, int], ...]] = (
    ("child", 2),
    ("school", 1),
    ("hospital", 2),
    ("accident", 2),
    ("injur", 2),
    ("died", 3),
    ("death", 3),
    ("elderly", 1),
    ("ambulance", 2),
    ("huge", 1),
    ("very big", 1),
    ("massive", 1),
    ("dangerous", 1),
    ("deep", 1),
    ("months", 1),
    ("weeks", 1),
    ("repeatedly", 1),
    ("again", 1),
)

#: Where each category starts before escalation.
_BASE_SEVERITY: Final[dict[str, int]] = {
    "pothole": 3,
    "waterlogging": 3,
    "drain_overflow": 3,
    "sewage": 3,
    "road_damage": 3,
    "garbage": 2,
    "streetlight": 2,
    "water_supply": 3,
    "tree_hazard": 3,
    "stray_animals": 2,
    "traffic_obstruction": 2,
    "public_toilet": 2,
    "encroachment": 2,
    "other": 2,
}

_LANDMARK_PATTERN: Final = re.compile(
    r"\b(?:near|outside|opposite|in front of|behind|next to|beside|at)\s+"
    r"((?:the\s+)?[A-Za-z0-9'&.\- ]{3,60}?)"
    r"(?=[,.;]|\s+(?:and|but|the water|there|it|is|was|has)\b|$)",
    re.IGNORECASE,
)

_WORD = re.compile(r"[a-z0-9]+")


class HeuristicProvider:
    """Deterministic lexical baseline. See the module docstring first."""

    name = "heuristic-baseline"
    #: No transcribe, no describe_image. Callers must check before asking.
    capabilities = frozenset({"extract", "embed"})

    # -- extraction -------------------------------------------------------

    def extract(
        self,
        *,
        text: str | None,
        transcript: str | None = None,
        vision_labels: list[str] | None = None,
    ) -> Extraction:
        """Classify by keyword. Vision labels are folded in if a caller supplies
        them, but this provider never produces them itself."""
        parts = [p for p in (text, transcript) if p]
        if vision_labels:
            parts.append(" ".join(vision_labels))

        if not parts:
            return Extraction(
                category="other",
                severity_raw=1,
                summary="Report carried no usable text",
                confidence=0.0,
                rationale="No text, transcript or vision labels to classify.",
            )

        blob = " ".join(parts)
        haystack = blob.lower()

        category, matched = self._category(haystack)
        hazards = self._hazards(haystack)
        severity = self._severity(category, haystack, hazards)

        # A keyword match is weak evidence; no match is weaker still.
        confidence = _BASELINE_CONFIDENCE_CAP if matched else 0.2

        return Extraction(
            category=category,
            subcategory=None,
            severity_raw=severity,
            hazard_flags=hazards,
            summary=self._summary(blob),
            landmark_text=self._landmark(blob),
            confidence=confidence,
            rationale=(
                f"Lexical baseline (not a model): category from keyword {matched!r}; "
                if matched
                else "Lexical baseline (not a model): "
                "no category keyword matched, defaulted to 'other'; "
            )
            + f"severity {severity}; hazards {hazards or 'none'}.",
        )

    @staticmethod
    def _category(haystack: str) -> tuple[str, str | None]:
        for category, keywords in _CATEGORY_KEYWORDS:
            for keyword in keywords:
                if keyword in haystack:
                    # A manhole is a hazard flag, not a category of its own; the
                    # reportable category is whatever else the text describes.
                    if category == "open_manhole_marker":
                        continue
                    return category, keyword
        return "other", None

    @staticmethod
    def _hazards(haystack: str) -> list[str]:
        return sorted(
            flag
            for flag, keywords in _HAZARD_KEYWORDS.items()
            if any(keyword in haystack for keyword in keywords)
        )

    @staticmethod
    def _severity(category: str, haystack: str, hazards: list[str]) -> int:
        severity = _BASE_SEVERITY.get(category, 2)
        for keyword, bump in _SEVERITY_ESCALATORS:
            if keyword in haystack:
                severity += bump
        if hazards:
            severity += 1
        return max(1, min(5, severity))

    @staticmethod
    def _summary(text: str) -> str:
        """First 20 words (PRD section 7/A1 caps the summary there)."""
        words = text.split()
        summary = " ".join(words[:20])
        return summary[:200] if summary else "Report with no usable text"

    @staticmethod
    def _landmark(text: str) -> str | None:
        match = _LANDMARK_PATTERN.search(text)
        if not match:
            return None
        landmark = match.group(1).strip(" .,;")
        return landmark or None

    # -- embedding --------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """A hashed character-n-gram vector. Lexical, not semantic.

        Character 3-grams plus whole words, hashed into 768 buckets with a
        signed contribution (the hashing trick), then L2-normalised so cosine
        similarity is a dot product. Shared wording scores high; paraphrase
        with no shared substrings scores near zero, which is the ceiling of
        what a bag-of-n-grams can do.
        """
        vector = [0.0] * EMBEDDING_DIMENSIONS
        normalised = " ".join(_WORD.findall(text.lower()))
        if not normalised:
            return vector

        features = _WORD.findall(normalised)
        features.extend(normalised[i : i + 3] for i in range(max(0, len(normalised) - 2)))

        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            position = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[position] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    # -- capabilities this baseline does not have -------------------------

    def transcribe(self, audio: bytes, *, language_hint: str | None = None) -> Transcript:
        raise CapabilityUnavailable(self.name, "transcribe")

    def describe_image(self, image: bytes) -> VisionResult:
        raise CapabilityUnavailable(self.name, "describe_image")
