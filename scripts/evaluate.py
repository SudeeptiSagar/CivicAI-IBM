"""Sentinel L4 golden-set regression gate (PRD section 8.1/14, P4/ROADMAP Lane 3).

    python -m scripts.evaluate                 print metrics, compare to baseline
    python -m scripts.evaluate --record         overwrite the recorded baseline

Two independent checks, both required for "a deliberately broken agent build
is blocked by CI" (PRD milestone M3's acceptance criterion):

1. **Meta-check** (`_run_meta_check`): every known-bad payload in
   `tests/test_structural.py`'s `BAD_ENVELOPES`/`BAD_PAYLOADS` tables must
   still fail L1. If Sentinel ever *passes* one, this exits non-zero
   immediately — that is a broken Sentinel build, not a metrics regression,
   and no threshold comparison is meaningful once verification itself is
   broken. This is the seed of the PRD 8.2 meta-check; the full version
   (self-alert, degrade to `strict`) is P6.
2. **Golden replay** (`_run_golden_eval`): replays
   `agents/av_sentinel/goldens/reports.json` — ~50 hand-labelled reports, 15
   known duplicate clusters — and computes the PRD section 14 metrics that
   apply without a live model: dedup recall, false-merge rate, and
   compression ratio. Category accuracy is reported too, but is informational
   only (see "What this does not do").

## What this does not do

This is an **offline replay of the scoring formula**, not a run through the
deployed pipeline. It imports `common.matching`'s exact thresholds, weights
and category radii/windows and recomputes spatial/temporal/semantic scores
directly from the golden fixtures (haversine distance instead of PostGIS
`ST_Distance`, the same `HeuristicProvider.embed()` cosine similarity instead
of pgvector) — so the *thresholds being tested are real*, but A0-A3 never run,
nothing touches Postgres or Redis, and no `verification_results` row is
written. A full end-to-end replay (through `IntakeAgent`, `PerceptionAgent`,
`DedupAgent`, `SynthesisAgent`, the way `scripts/seed_m1.py --drain` does)
would additionally catch a regression in those agents' own code paths, not
just in the scoring formula; that is a larger integration harness than P4
budgets for, and is worth calling out rather than silently claiming this
script already does it.

`common.llm.heuristic.HeuristicProvider` classifies category and produces the
embedding used for the semantic component. Per `docs/perception-and-dedup.md`,
its embedding is lexical, not semantic — and on this golden set the measured
result is not "below the PRD's 0.80 target", it is **`dedup_recall: 0.0`**.
Every genuine duplicate pair's semantic score lands in the 0.60-0.82 grey
zone the caveat describes, and `spatial`/`temporal`/`semantic` combined at
their published weights never clears `AUTO_LINK_THRESHOLD` (0.82) for any
pair in this fixture, so nothing auto-links. This is not a bug in this
script; it is exactly the documented gap, now measured rather than described.
`category_accuracy` (0.73 on the recorded baseline) is far more usable, since
keyword matching is closer to what the lexical baseline is actually good at.

This script does not enforce the PRD's absolute targets, only regression
against the last recorded baseline (`agents/av_sentinel/goldens/baseline.json`)
— which is what the acceptance criterion actually asks for ("blocked on
regression", not "blocked below the PRD target"), and what makes this gate
meaningful even while the baseline itself is this far from PRD 14's goals: a
real provider (Lane 4) should move `dedup_recall` up, and this script will
catch it moving back down. Category accuracy excludes the 4 Kannada/Hindi
fixtures, whose `expected_category` is deliberately `null` — see
`agents/av_sentinel/goldens/reports.json`'s `_language_note`.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any

GOLDENS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "agents"
    / "av_sentinel"
    / "goldens"
    / "reports.json"
)
BASELINE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "agents"
    / "av_sentinel"
    / "goldens"
    / "baseline.json"
)

#: How far a metric may regress from the recorded baseline before this exits
#: non-zero. Small enough to catch a real regression, large enough that two
#: runs of the same deterministic code do not flap (there is no randomness
#: here, but future goldens edits should not need baseline surgery for noise).
TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class Metrics:
    reports: int
    predicted_clusters: int
    compression_ratio: float
    dedup_recall: float
    false_merge_rate: float
    category_accuracy: float
    category_accuracy_n: int


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


class _UnionFind:
    """Predicted-cluster bookkeeping. Union-by-rank, no path-splitting drama."""

    def __init__(self, ids: list[str]) -> None:
        self._parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def clusters(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for node in self._parent:
            groups.setdefault(self.find(node), []).append(node)
        return groups


def _load_goldens() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
    return doc


def _predict_links(reports: list[dict[str, Any]]) -> _UnionFind:
    """Replay A2's scoring formula pairwise (see module docstring)."""
    from common.llm.heuristic import HeuristicProvider
    from common.matching import AUTO_LINK_THRESHOLD, WEIGHTS, bounds_for

    provider = HeuristicProvider()
    uf = _UnionFind([r["id"] for r in reports])

    # Category and embedding, once per report - not per pair.
    extracted: dict[str, tuple[str, list[float]]] = {}
    for r in reports:
        extraction = provider.extract(text=r["text"])
        embedding = provider.embed(r["text"])
        extracted[r["id"]] = (extraction.category, embedding)

    for a, b in combinations(reports, 2):
        cat_a, emb_a = extracted[a["id"]]
        cat_b, emb_b = extracted[b["id"]]
        if cat_a != cat_b:
            # A2 never merges across categories (PRD section 7/A2).
            continue

        radius_m, window_days = bounds_for(cat_a)
        distance_m = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        spatial = max(0.0, 1.0 - distance_m / radius_m) if radius_m > 0 else 0.0

        age_days = abs(a["hours_ago"] - b["hours_ago"]) / 24.0
        temporal = max(0.0, 1.0 - age_days / window_days) if window_days > 0 else 0.0

        semantic = _cosine_similarity(emb_a, emb_b)

        # No visual component in this offline replay either - no photos in
        # the golden fixtures, same as A2's own renormalisation when a
        # component is unavailable (common.matching.Candidate.score).
        components = {"spatial": spatial, "temporal": temporal, "semantic": semantic}
        total_weight = sum(WEIGHTS[k] for k in components)
        score = sum(WEIGHTS[k] * v for k, v in components.items()) / total_weight

        if score >= AUTO_LINK_THRESHOLD:
            uf.union(a["id"], b["id"])

    return uf


def _category_accuracy(reports: list[dict[str, Any]]) -> tuple[float, int]:
    from common.llm.heuristic import HeuristicProvider

    provider = HeuristicProvider()
    labelled = [r for r in reports if r.get("expected_category") is not None]
    if not labelled:
        return 0.0, 0
    correct = 0
    for r in labelled:
        extraction = provider.extract(text=r["text"])
        if extraction.category == r["expected_category"]:
            correct += 1
    return correct / len(labelled), len(labelled)


def _expected_pairs(reports: list[dict[str, Any]]) -> set[frozenset[str]]:
    by_cluster: dict[str, list[str]] = {}
    for r in reports:
        cluster = r.get("expected_cluster")
        if cluster is not None:
            by_cluster.setdefault(cluster, []).append(r["id"])
    pairs: set[frozenset[str]] = set()
    for members in by_cluster.values():
        for a, b in combinations(members, 2):
            pairs.add(frozenset((a, b)))
    return pairs


def _predicted_pairs(clusters: dict[str, list[str]]) -> set[frozenset[str]]:
    pairs: set[frozenset[str]] = set()
    for members in clusters.values():
        if len(members) < 2:
            continue
        for a, b in combinations(members, 2):
            pairs.add(frozenset((a, b)))
    return pairs


def compute_metrics() -> Metrics:
    doc = _load_goldens()
    reports = doc["reports"]

    uf = _predict_links(reports)
    clusters = uf.clusters()

    expected_pairs = _expected_pairs(reports)
    predicted_pairs = _predicted_pairs(clusters)

    true_positive_pairs = expected_pairs & predicted_pairs
    dedup_recall = len(true_positive_pairs) / len(expected_pairs) if expected_pairs else 1.0

    false_positive_pairs = predicted_pairs - expected_pairs
    false_merge_rate = len(false_positive_pairs) / len(predicted_pairs) if predicted_pairs else 0.0

    category_accuracy, category_n = _category_accuracy(reports)

    return Metrics(
        reports=len(reports),
        predicted_clusters=len(clusters),
        compression_ratio=round(len(reports) / len(clusters), 4) if clusters else 0.0,
        dedup_recall=round(dedup_recall, 4),
        false_merge_rate=round(false_merge_rate, 4),
        category_accuracy=round(category_accuracy, 4),
        category_accuracy_n=category_n,
    )


def _run_meta_check() -> list[str]:
    """Replay tests/test_structural.py's known-bad fixtures. See module docstring."""
    from agents.av_sentinel.layers.structural import verify_structural
    from tests.test_structural import BAD_ENVELOPES, BAD_PAYLOADS

    failures: list[str] = []
    for name, envelope in [*BAD_ENVELOPES, *BAD_PAYLOADS]:
        verdict = verify_structural(envelope)
        if verdict.passed:
            failures.append(name)
    return failures


def _regressed(current: Metrics, baseline: dict[str, Any]) -> list[str]:
    problems = []
    if current.dedup_recall < baseline["dedup_recall"] - TOLERANCE:
        problems.append(
            f"dedup_recall regressed: {current.dedup_recall} < baseline {baseline['dedup_recall']}"
        )
    if current.false_merge_rate > baseline["false_merge_rate"] + TOLERANCE:
        problems.append(
            f"false_merge_rate regressed: {current.false_merge_rate} > "
            f"baseline {baseline['false_merge_rate']}"
        )
    if current.category_accuracy < baseline["category_accuracy"] - TOLERANCE:
        problems.append(
            f"category_accuracy regressed: {current.category_accuracy} < "
            f"baseline {baseline['category_accuracy']}"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record", action="store_true", help="overwrite the recorded baseline with this run"
    )
    parser.add_argument(
        "--skip-meta-check",
        action="store_true",
        help="skip the known-bad-fixture replay (metrics only)",
    )
    args = parser.parse_args(argv)

    if not args.skip_meta_check:
        failures = _run_meta_check()
        if failures:
            print("META-CHECK FAILED: Sentinel L1 passed payloads it must reject:")
            for name in failures:
                print(f"  - {name}")
            return 1
        print("meta-check: ok (every known-bad fixture still fails L1)")

    metrics = compute_metrics()
    print(json.dumps(asdict(metrics), indent=2))

    if args.record:
        BASELINE_PATH.write_text(json.dumps(asdict(metrics), indent=2) + "\n", encoding="utf-8")
        print(f"recorded baseline -> {BASELINE_PATH}")
        return 0

    if not BASELINE_PATH.exists():
        print(f"no recorded baseline at {BASELINE_PATH}; run with --record first")
        return 1

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    problems = _regressed(metrics, baseline)
    if problems:
        print("REGRESSION vs recorded baseline:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("no regression vs recorded baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
