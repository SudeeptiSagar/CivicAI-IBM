"""The Sentinel golden-set regression gate (`scripts/evaluate.py`, P4).

Runs the same offline replay CI would run, with no database or bus. Covers
the meta-check (known-bad fixtures must still fail L1), the golden set's own
shape, and that the metrics computed today match the recorded baseline —
which is itself a regression test: if this fails, either a real regression
was introduced, or the golden set changed and `--record` needs running (and
this test needs its expected numbers updated deliberately, not silently).
"""

from __future__ import annotations

import json

from scripts.evaluate import (
    BASELINE_PATH,
    GOLDENS_PATH,
    Metrics,
    _regressed,
    _run_meta_check,
    compute_metrics,
)

# -- the golden set's own shape --------------------------------------------


def test_goldens_file_has_roughly_fifty_reports() -> None:
    doc = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
    assert 45 <= len(doc["reports"]) <= 55


def test_goldens_file_has_fifteen_duplicate_clusters() -> None:
    doc = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
    assert len(doc["duplicate_clusters"]) == 15


def test_goldens_includes_non_english_reports() -> None:
    """Roadmap Lane 3: Kannada and Hindi, deliberately absent from m1_reports.json."""
    doc = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
    langs = {r["lang"] for r in doc["reports"]}
    assert "kn" in langs
    assert "hi" in langs


def test_non_english_reports_have_no_expected_category() -> None:
    """Nothing in this build can honestly classify them; a fixture that guessed
    one would misrepresent what the pipeline can actually do."""
    doc = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
    for r in doc["reports"]:
        if r["lang"] in ("kn", "hi"):
            assert r["expected_category"] is None
            assert "note" in r


def test_every_report_id_is_unique() -> None:
    doc = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
    ids = [r["id"] for r in doc["reports"]]
    assert len(ids) == len(set(ids))


def test_every_cluster_member_id_exists_as_a_report() -> None:
    doc = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
    report_ids = {r["id"] for r in doc["reports"]}
    for cluster in doc["duplicate_clusters"]:
        for member_id in cluster["member_ids"]:
            assert member_id in report_ids


# -- the meta-check ----------------------------------------------------------


def test_meta_check_finds_no_failures_today() -> None:
    """Every known-bad fixture in test_structural.py must still fail L1."""
    assert _run_meta_check() == []


# -- metrics vs the recorded baseline ---------------------------------------


def test_baseline_file_exists() -> None:
    assert BASELINE_PATH.exists(), "run `python -m scripts.evaluate --record` first"


def test_current_metrics_do_not_regress_the_recorded_baseline() -> None:
    metrics = compute_metrics()
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert _regressed(metrics, baseline) == []


def test_metrics_are_deterministic_across_runs() -> None:
    """No randomness anywhere in the replay: two runs must agree exactly."""
    assert compute_metrics() == compute_metrics()


def test_compute_metrics_returns_the_documented_shape() -> None:
    metrics = compute_metrics()
    assert isinstance(metrics, Metrics)
    assert metrics.reports > 0
    assert 0.0 <= metrics.dedup_recall <= 1.0
    assert 0.0 <= metrics.false_merge_rate <= 1.0
    assert 0.0 <= metrics.category_accuracy <= 1.0
    # Kannada/Hindi fixtures are excluded from category accuracy (see module
    # docstring); everything else on the golden set is included.
    assert metrics.category_accuracy_n == metrics.reports - 4
