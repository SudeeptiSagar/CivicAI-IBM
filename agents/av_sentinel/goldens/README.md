# Sentinel golden set

PRD section 8.1 (L4) and ROADMAP Lane 3. `reports.json` holds ~50 hand-labelled
reports and 15 known duplicate clusters, in the same style as
`data/seed/m1_reports.json`; `baseline.json` is the last recorded run of
`scripts.evaluate`, replayed and compared on every deploy.

This directory lives under `agents/av_sentinel/`, not `data/goldens/`, per the
repository structure in PRD section 11 (`av_sentinel/{layers,rules,goldens}`).
ROADMAP's Lane 3 section names `data/goldens/` for the same deliverable; this
is the one place the two documents disagree, and PRD section 11's explicit
tree wins since it is the more specific source. Nothing else changes: the
format matches what Lane 3 describes.

Regenerate the baseline after a deliberate, reviewed change to the scoring
formula or the golden set itself:

```bash
python -m scripts.evaluate --record
```

Never regenerate it just to make a CI failure go away — that defeats the
point of a regression gate. See `scripts/evaluate.py`'s module docstring for
what is and is not measured here, including the honest current number for
`dedup_recall` (0.0, not merely "below target" — see that docstring for why).
