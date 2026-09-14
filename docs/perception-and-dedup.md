# Perception and deduplication

How A1, A2 and A3 actually behave in this build, and where they differ from
what the PRD describes. Read this before drawing any conclusion from a dedup
number.

---

## The reasoning provider

PRD section 6.2 puts IBM watsonx (Granite) behind `llm/provider.py` and forbids
any agent importing a vendor SDK. The interface exists and is honoured. **No
watsonx credentials do** (PRD open question 2), and `get_provider("watsonx")`
raises `NotImplementedError` rather than returning a stub.

What runs instead is `common/llm/heuristic.py`: a **deterministic lexical
baseline, not a model**. Keyword matching for category and hazard flags, small
rules for severity, and a hashed character-n-gram vector for `embed()`.

### Why a baseline exists at all

Without something implementing the protocol, A1 cannot run, so A2 has no input
and A3 has no incidents — the entire core path would sit untested behind a
missing API key, and M1 could not be demonstrated.

The baseline also gives the eval harness a floor. A dedup rate a keyword
matcher already achieves is not evidence that a model is working, and without a
baseline there is nothing to make that comparison against.

### What it cannot do, and what that costs

| Capability | Baseline | Consequence |
|---|---|---|
| Category from text | keyword match | English only; Kannada and Hindi fall to `other` (PRD open question 3) |
| Embedding | hashed char n-grams | **lexical, not semantic** — see below |
| ASR | none | audio is stored, never transcribed |
| Vision | none | photos are stored, never examined |
| Adjudication | none | A3's grey zone is resolved deterministically |

Confidence is capped at 0.55 whatever it matched, so downstream agents and
Sentinel treat these extractions as the weak evidence they are.

## A1 degrades openly

When a modality is present but unreadable, A1 records the gap, lowers its
confidence, and names it in the rationale. What it never does is emit
`vision_labels: []`, because a consumer cannot distinguish that from "a photo
was examined and nothing was found" — and A2 would weight the result as if the
image had been read.

With no readable modality at all, A1 emits a `*.skipped` event rather than
guessing.

**Modality conflict** (PRD section 7/A1: when the image and the text disagree,
lower confidence and flag rather than picking one) is implemented and tested,
but cannot fire here — it needs vision labels to disagree with. Note that each
modality is classified *separately* before comparison: handing text and vision
to a single `extract()` call lets the provider reconcile them internally, and
the disagreement the PRD wants surfaced disappears.

## The semantic component is the weak link

A2 scores four components. Three behave as the PRD intends. The fourth does not:

```
spatial   PostGIS distance          measured, reliable
temporal  report age                measured, reliable
semantic  cosine of A1 embeddings   LEXICAL ONLY in this build
visual    image comparison          unavailable (no vision)
```

On the M1 fixture, genuine duplicates score:

```
spatial 0.79-0.95   temporal 0.78-0.99   semantic 0.39-0.67
```

Spatial and temporal are strong and correct. Semantic tops out around 0.67 for
paraphrases of one pothole, where a real embedding would score 0.85+. The
result is that most genuine duplicates land in the **0.60-0.82 grey zone**
rather than clearing the 0.82 auto-link threshold.

**This is the single clearest thing a real provider would improve.** Dedup
recall should be expected to rise materially once one is configured, and the
grey zone should empty out.

### Weight renormalisation

The visual component is null whenever either report lacks a photo — in this
build, always. Scoring a missing component as zero would drag every match below
threshold and defeat dedup entirely, so the available weights are renormalised
to sum to 1. `component_scores` reports the missing one as `null`, so the
arithmetic stays auditable.

## A3 arbitrates the grey zone deterministically

PRD section 7/A2 sends the grey zone to A3 "with an LLM adjudication call". No
provider offers adjudication, so a deterministic adjudicator stands in:
spatial >= 0.75 **and** temporal >= 0.75 **and** semantic >= 0.25.

The substitution is principled. The grey zone exists because the semantic
signal is uncertain; spatial and temporal do not depend on a model at all. The
0.75 floors mean "inside the inner quarter of this category's own radius and
window" — for a pothole, 19 m and 5 days. PRD section 15 endorses the ordering
explicitly: "cheap deterministic gates first; LLM only in the grey zone".

Anything the adjudicator declines does not merge. PRD section 15 rates false
merges as high-impact, and a duplicate incident is cheaper than a hidden one.

## A2 proposes, A3 disposes

A2 scores a report against the incidents that exist *when it looks*. A3 owns
the `incidents` table, so its view is authoritative — and under concurrency the
two disagree.

This was a real bug, found by running the live stack rather than by a test.
Four citizens reporting one pothole within a second produce four reports with
four different `correlation_id`s. PRD section 9.4 orders messages per
`correlation_id` only, so A2 scored all four before A3 had created an incident
for any of them: every one saw an empty table, every one seeded. Four reports,
four incidents — the exact opposite of the PRD's opening promise.

A3 now re-runs the same scoring (`common/matching.py`, shared by both agents)
against current state before seeding anything new, applying the same bar. It
can only ever turn a seed into a join, never the reverse. Both directions are
covered by regression tests in `tests/test_pipeline_m1.py`.

## Measured on the M1 fixture

20 seeded reports collapse to exactly the 11 expected incidents, including the
deliberate traps: a same-category pothole 4 km away stays separate, drainage
and waterlogging 70 m apart stay separate because A2 never merges across
categories, and a cluster with 3 reports from 2 devices reports
`distinct_reporters = 2`.

**Compression is 1.82x, below the >= 3x target in PRD section 14.** That is a
property of the fixture, not a measurement of dedup quality: it is deliberately
stuffed with singletons to test that A2 does *not* over-merge. A fixture with
realistic duplicate density would be the right place to measure the PRD metric,
and the golden set in P4 is where that belongs.

## Not implemented in P2

* **Reverse geocoding.** PRD section 7/A0 lists "device GPS -> reverse geocode
  -> ward lookup". Ward lookup runs against local polygons; there is no
  geocoder, so reports carry coordinates and a ward, not a street address.
* **Location refinement from landmarks.** PRD section 7/A1 refines location
  from landmark text above 50 m GPS accuracy. The landmark is extracted and
  surfaced; turning it into coordinates needs a place lookup, so A1 leaves the
  location alone rather than inventing precision.
* **Perceptual hashing.** The visual component needs images to compare and a
  provider to compare them.
* **A3 pattern mode / SuperIncidents.** P5 (M4), by design.
