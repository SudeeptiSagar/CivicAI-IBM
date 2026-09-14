# CivicAI

**CivicAI transforms thousands of fragmented citizen complaints into a smaller set of verified, prioritized civic incidents, helping authorities understand what is actually happening in the city and what needs attention first.**

[`PRD.md`](PRD.md) is the source of truth for requirements, architecture, agent
responsibilities, message contracts, data model and acceptance criteria. This
README covers only how to work in the repo and what actually exists today.

---

## Status: P1 complete — M0 met

> **M0 done when:** "An event flows A0 → echo → Sentinel L1 and is visible in the trace viewer" (PRD §13)

That now runs on the real stack: Redis Streams, Postgres with PostGIS and
pgvector, the echo agent, Sentinel, and the trace endpoint.

```
$ docker compose run --rm api python -m scripts.emit_report
published reports.ingested trace_id=01a09f9a-a590-79f1-8221-6861855bf0ac

$ curl localhost:8000/v1/trace/01a09f9a-a590-79f1-8221-6861855bf0ac
message_count: 2  verdict_count: 2  edges: 1  roots: 1
  reports.ingested          agent=A0    verdicts=['pass']  runs=['skipped']
  reports.ingested.skipped  agent=ECHO  verdicts=['pass']
```

### What exists

| Component | Status |
|---|---|
| `schemas/` — envelope + 15 topic schemas + 3 patterned families | Complete |
| `common/` — envelope, UUIDv7 ids, schema registry, config, logging, db, message archive | Complete |
| `bus/` — `Bus` interface, in-memory double, **Redis Streams transport** | Complete |
| `db/` — migration runner + full schema for PRD §10 (plus 4 documented additions) | Complete |
| `agents/base.py` — the PRD §7 agent contract, executable | Complete |
| `agents/echo`, `agents/av_sentinel` — M0 smoke agent, Sentinel **L1** | Complete |
| `api/` — `/v1/health`, `/v1/health/agents`, `/v1/trace/{id}` | Complete |
| `docker-compose.yml` — Postgres+PostGIS+pgvector, Redis, MinIO, API, agents | Complete |
| `tests/` — 348 tests | Passing |

### What does not exist yet

Named explicitly so nothing reads as more finished than it is:

- **No real agents.** A0–A7 are not implemented. The echo agent is a pipeline
  smoke test that emits a `*.skipped` event; it has no business logic, by design.
- **No LLM provider.** `common/llm/provider.py` does not exist and no watsonx
  credentials are configured. No agent fabricates reasoning output in its
  absence.
- **Sentinel runs L1 only**, and **does not yet gate consumers** — see
  [Known limitations](#known-limitations).
- **No object-store client.** MinIO is provisioned and its bucket created, but
  nothing writes blobs until A0 handles media in P2.
- **No write endpoints.** `POST /v1/reports` and the resolution endpoints belong
  to the agents that own those tables (A0 in P2, A6 in P5).
- **No frontend.** The trace viewer is the JSON endpoint; the UI is P7.

---

## Known limitations

**Sentinel verifies alongside consumers, not in front of them.** PRD §8 calls
for downstream agents to act only on events carrying a valid verdict. Today
Sentinel and each agent hold independent consumer groups on the same topic, so
an agent can begin work on a message Sentinel is about to quarantine. The
effective behaviour is `permissive` with a zero deadline regardless of
`CIVICAI_SENTINEL_MODE`, and `/v1/health/agents` reports this honestly as
`sentinel_gate_enforced: false`. Closing it is P4 (M3), whose acceptance
criterion requires a real gate. Details in [`docs/sentinel.md`](docs/sentinel.md).

---

## Running it

Needs Docker. The stack is Postgres 16 + PostGIS + pgvector (one custom image —
no published image carries both), Redis, and MinIO.

```bash
cp .env.example .env

docker compose up -d postgres redis minio minio-init
docker compose run --rm migrate              # apply migrations
docker compose up -d api agent-sentinel agent-echo

curl localhost:8000/v1/health
```

Then push an event through:

```bash
docker compose run --rm api python -m scripts.emit_report --text "pothole by the school"
curl "localhost:8000/v1/trace/<trace_id>"
```

`scripts/emit_report.py` is a development harness standing in for A0 until P2.
It mints a well-formed envelope and nothing more — no media handling, no EXIF
stripping, no reverse geocoding, no rate limiting.

Tear down with `docker compose down -v`.

---

## Developing

Requires **Python 3.13** (3.14 is not supported — several dependencies have no
wheels for it).

```bash
py -V:3.13 -m venv .venv                          # Windows
python3.13 -m venv .venv                          # macOS / Linux

.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
.venv/bin/python -m pip install -e ".[dev]"       # macOS / Linux
```

### Verify

All four must pass before a commit. CI runs exactly these.

```bash
ruff check .
ruff format --check .
mypy
pytest
```

Integration tests **skip** when Postgres and Redis are unreachable, so `pytest`
stays green with nothing running — and a skip is reported as a skip, never as a
pass. With the stack up they run against a separate `civicai_test` database and
Redis index 15, so a test run can never touch your dev data.

### Layout

```
schemas/    JSON Schema per topic — the contract source of truth (PRD §11)
common/     envelope, ids, schema registry, config, logging, db, message archive
bus/        transport abstraction: Redis Streams, plus an in-memory double
db/         migrations and the runner
agents/     base.py is the PRD §7 contract; av_sentinel verifies all the others
api/        FastAPI read surface
scripts/    development harnesses
tests/      factories.py holds a valid example message for every topic
```

Further reading: [`docs/message-contracts.md`](docs/message-contracts.md),
[`docs/data-model.md`](docs/data-model.md), [`docs/sentinel.md`](docs/sentinel.md).

---

## The three rules worth knowing before you write an agent

**Every message carries a trace.** `trace_id` is minted once by A0 and
propagated unchanged downstream, so any incident can be replayed back to the
original citizen submission. `causation_id` points at the message that caused
this one, which is what rebuilds the decision DAG. `Envelope.originate()` and
`parent.derive()` handle both for you.

**Delivery is at-least-once, so handlers must be idempotent.** The bus will
redeliver. The runtime keys on `envelope.idempotency_key()` and records the
outcome in `handler_results`, making a repeat a no-op that returns the prior
result (PRD §9.4).

**Whoever publishes, archives.** `common.messagelog.archive()` before
`bus.publish()`, always in that order — a message a consumer can see must
already be in the audit trail. `Agent.emit()` does this for you; only reach for
`bus.publish()` directly if you have a reason.

## Writing an agent

Subclass `Agent` and implement `handle()`. The runtime gives you consumer
groups, idempotency, the retry budget, deadlettering, archiving and run
telemetry.

```python
class PerceptionAgent(Agent):
    name = "A1"
    version = "1.0.0"
    input_topic = "reports.ingested"

    def handle(self, envelope: Envelope) -> list[Envelope]:
        if not envelope.payload["has_photo"] and not envelope.payload["raw_text"]:
            raise SkipSignal("no_media_and_no_text", "nothing to extract")

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

Raise `SkipSignal` when there is genuinely nothing to produce — it emits a
`*.skipped` event carrying the reason, which is the honest alternative to
inventing output. Any other exception is retried three times with backoff, then
deadlettered.

## Adding a topic

1. Add it to `PRODUCERS` in `common/topics.py`.
2. Add `schemas/<topic>.v1.json`, composing the envelope via `allOf` and pinning
   `topic` to a `const`.
3. Add a factory entry in `tests/factories.py`.

The schema tests enforce the rest — a topic without a schema, a schema without
an envelope reference, or a topic without a factory all fail the build.
