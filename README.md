# CivicAI

**CivicAI transforms thousands of fragmented citizen complaints into a smaller set of verified, prioritized civic incidents, helping authorities understand what is actually happening in the city and what needs attention first.**

[`PRD.md`](PRD.md) is the source of truth for requirements, architecture, agent
responsibilities, message contracts, data model and acceptance criteria. This
README covers only how to work in the repo and what actually exists today.

**Picking this up for the first time? Start with [`ROADMAP.md`](ROADMAP.md)** —
what is built, what is not, what to do next, and what can be worked on in
parallel.

---

## Status: P2 complete — M0 and M1 met

> **M1 done when:** "20 seeded reports collapse into the expected incidents" (PRD §13)

The core path runs end to end: a citizen POSTs a report, A0 validates and
stores it, A1 extracts, A2 deduplicates against PostGIS and pgvector, A3
consolidates into incidents, and Sentinel verifies every message.

```
$ curl -X POST localhost:8000/v1/reports \
    -F device_hash=citizen-1 -F lat=12.9345 -F lon=77.6101 -F gps_accuracy_m=9 \
    -F text="Huge pothole outside the school gate, very deep and dangerous"
{"report_id":"…","trace_id":"…","ward_id":"BLR-151","matched_incident":null}

# four citizens, submitted in parallel, one pothole:
$ psql -c "select category, report_count, distinct_reporters from incidents"
 pothole |     4 |      4
```

### What exists

| Component | Status |
|---|---|
| `schemas/` — envelope + 15 topic schemas + 3 patterned families | Complete |
| `common/` — envelope, ids, schemas, db, geo, storage, matching, LLM interface | Complete |
| `bus/` — `Bus` interface, in-memory double, Redis Streams | Complete |
| `db/` — migration runner + PRD §10 schema + reference geography | Complete |
| `agents/base.py` — the PRD §7 agent contract, executable | Complete |
| **A0 Intake** — validation, EXIF strip, ward join, rate limiting | Complete |
| **A1 Perception** — extraction + embedding, degrades openly | Complete (text only) |
| **A2 Dedup** — 4-component scoring over PostGIS + pgvector | Complete |
| **A3 Synthesis** — consolidation, weighted centroid, adjudication | Consolidation only |
| `agents/av_sentinel` — Sentinel **L1** | Complete |
| `api/` — reports, report status, health, trace | Complete |
| `tests/` — 493 tests | Passing |

### What does not exist yet

- **No real model.** No watsonx credentials (PRD open question 2). A
  deterministic lexical baseline stands in — see below.
- **No ASR, no vision.** Audio and photos are stored but never read.
- **A3 pattern mode / SuperIncidents** — P5.
- **A4 Prioritization, A5 Routing, A6 Resolution, A7 Evidence** — P3 and P5.
- **Sentinel L2/L3/L4**, and Sentinel does not yet gate consumers — P4.
- **No frontend.** The trace viewer is a JSON endpoint; the UI is P7.

---

## Known limitations

Three things are true of this build that a reader could otherwise mistake.

**1. The reasoning provider is not a model.** `common/llm/heuristic.py` is a
deterministic lexical baseline: keyword matching plus a hashed character-n-gram
vector. It exists so the core path is runnable and measurable without
credentials, and it caps its own confidence at 0.55. Its embedding is *lexical,
not semantic*, which systematically depresses A2's semantic component and is the
single clearest thing a real provider would improve. Details and measured
numbers in [`docs/perception-and-dedup.md`](docs/perception-and-dedup.md).

**2. Faces and plates are not blurred.** PRD §15 asks for it; it needs a
detector this build does not have. EXIF stripping *is* implemented and verified
against stored bytes, but a photo containing a bystander is stored with that
person legible. This is an open PII gap, not an oversight —
[`docs/media-handling.md`](docs/media-handling.md).

**3. Sentinel verifies alongside consumers, not in front of them.** An agent
can begin work on a message Sentinel is about to quarantine. `/v1/health/agents`
reports this as `sentinel_gate_enforced: false` rather than letting the
configured mode imply a guarantee — [`docs/sentinel.md`](docs/sentinel.md).

---

## Running it

Needs Docker. Postgres 16 + PostGIS + pgvector (one custom image — no published
image carries both), Redis, MinIO.

```bash
cp .env.example .env

docker compose up -d postgres redis minio minio-init
docker compose run --rm migrate            # schema
docker compose run --rm load-reference     # city and ward polygons
docker compose up -d api agent-sentinel agent-perception agent-dedup agent-synthesis

curl localhost:8000/v1/health
```

Submit a report, then follow it:

```bash
curl -X POST localhost:8000/v1/reports \
  -F device_hash=me -F lat=12.9345 -F lon=77.6101 -F gps_accuracy_m=9 \
  -F text="Huge pothole outside the school gate"

curl localhost:8000/v1/reports/<report_id>   # status + "N others reported this"
curl localhost:8000/v1/trace/<trace_id>      # every agent decision + verdicts
```

Or run the M1 scenario — 20 reports, 11 expected incidents:

```bash
docker compose run --rm api python -m scripts.seed_m1 --drain
```

Tear down with `docker compose down -v`.

---

## Developing

Requires **Python 3.13** (3.14 is not supported — dependency wheels).

```bash
py -V:3.13 -m venv .venv                          # Windows
python3.13 -m venv .venv                          # macOS / Linux
.venv/Scripts/python -m pip install -e ".[dev]"
```

### Verify

All four must pass before a commit. CI runs exactly these.

```bash
ruff check .
ruff format --check .
mypy
pytest
```

Integration tests **skip** when the stack is down, so `pytest` stays green with
nothing running — and a skip is reported as a skip, never as a pass. With the
stack up they run against a separate `civicai_test` database, Redis index 15,
and a `tests/` key prefix in the bucket, so they never touch dev data.

### Layout

```
schemas/    JSON Schema per topic — the contract source of truth (PRD §11)
common/     envelope, ids, db, geo, storage, matching, llm/ (provider interface)
bus/        transport abstraction: Redis Streams, plus an in-memory double
db/         migrations and the runner
agents/     base.py is the PRD §7 contract; a0..a3 and av_sentinel
api/        FastAPI
data/       reference geography and the M1 seed scenario
scripts/    migrations, reference loading, seeding harnesses
tests/      factories.py holds a valid example message for every topic
```

Further reading: [`docs/perception-and-dedup.md`](docs/perception-and-dedup.md),
[`docs/message-contracts.md`](docs/message-contracts.md),
[`docs/data-model.md`](docs/data-model.md),
[`docs/sentinel.md`](docs/sentinel.md),
[`docs/media-handling.md`](docs/media-handling.md).

---

## The four rules worth knowing before you write an agent

**Every message carries a trace.** `trace_id` is minted once by A0 and
propagated unchanged; `causation_id` points at the message that caused this one.
`Envelope.originate()` and `parent.derive()` handle both.

**Delivery is at-least-once, so handlers must be idempotent.** The runtime keys
on `envelope.idempotency_key()` and records the outcome in `handler_results`,
making a repeat a no-op returning the prior result (PRD §9.4).

**Whoever publishes, archives.** `common.messagelog.archive()` before
`bus.publish()`, always in that order. `Agent.emit()` does it for you.

**Never invent what you could not read.** If a modality has no provider, record
the gap, lower the confidence and say so — or raise `SkipSignal`. An agent that
emits a plausible default is indistinguishable from one that actually looked.

## Writing an agent

Subclass `Agent` and implement `handle()`. The runtime gives you consumer
groups, idempotency, the retry budget, deadlettering, archiving and telemetry.

```python
class PerceptionAgent(Agent):
    name = "A1"
    version = "1.0.0"
    input_topic = "reports.ingested"

    def handle(self, envelope: Envelope) -> list[Envelope]:
        if not envelope.payload["raw_text"]:
            raise SkipSignal("no_readable_modality", "nothing to extract")

        return [
            envelope.derive(
                topic="reports.understood",
                producer=self.producer,
                confidence=0.87,
                rationale="Pothole visible in image; transcript mentions school proximity.",
                payload={...},
            )
        ]
```

## Adding a topic

1. Add it to `PRODUCERS` in `common/topics.py`.
2. Add `schemas/<topic>.v1.json`, composing the envelope via `allOf` and pinning
   `topic` to a `const`.
3. Add a factory entry in `tests/factories.py`.

The schema tests enforce the rest — a topic without a schema, a schema without
an envelope reference, or a topic without a factory all fail the build.
