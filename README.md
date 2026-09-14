# CivicAI

**CivicAI transforms thousands of fragmented citizen complaints into a smaller set of verified, prioritized civic incidents, helping authorities understand what is actually happening in the city and what needs attention first.**

[`PRD.md`](PRD.md) is the source of truth for requirements, architecture, agent
responsibilities, message contracts, data model and acceptance criteria. This
README covers only how to work in the repo and what actually exists today.

---

## Status: P0 — contracts and foundation

P0 builds the layer every agent depends on: the message envelope, the JSON
Schemas that define each topic, the bus abstraction, and Sentinel's structural
verification layer. It runs with **no infrastructure** — no Docker, no Postgres,
no Redis — which is what lets CI verify it today.

### What exists

| Component | Status |
|---|---|
| `schemas/` — envelope + 15 topic schemas + 3 patterned families | Complete |
| `common/` — envelope, UUIDv7 ids, schema registry, config, logging, idempotency | Complete |
| `bus/` — `Bus` interface + in-memory implementation | Complete |
| `agents/av_sentinel/layers/structural.py` — Sentinel **L1** | Complete |
| `tests/` — 229 tests | Passing |

### What does not exist yet

Everything else. Named explicitly so nothing here reads as more finished than it is:

- **No agents.** A0–A7 are not implemented. `agents/` holds only Sentinel L1.
- **No Redis bus.** `bus/memory.py` is a faithful test double, not a transport.
  `bus/redis.py` arrives in P1.
- **No database, no object store, no API, no frontend.** No `docker-compose.yml`
  yet either — P1.
- **No LLM provider.** `common/llm/provider.py` does not exist. No watsonx
  credentials are configured, and no agent fabricates reasoning output in its
  absence: agents that need a provider will fail to `deadletter` rather than
  guess (PRD section 7).
- **Sentinel L2, L3 and L4** — invariants, LLM-as-judge, goldens and drift
  monitors — are P4 and P6. Only L1 runs today.

---

## Working in this repo

### Setup

Requires **Python 3.13** (3.14 is not supported — several dependencies have no
wheels for it yet).

```bash
py -V:3.13 -m venv .venv          # Windows
python3.13 -m venv .venv          # macOS / Linux

.venv/Scripts/python -m pip install -e ".[dev]"    # Windows
.venv/bin/python -m pip install -e ".[dev]"        # macOS / Linux

cp .env.example .env
```

### Verify

All four must pass before a commit. CI runs exactly these.

```bash
ruff check .
ruff format --check .
mypy
pytest
```

### Layout

```
schemas/    JSON Schema per topic — the contract source of truth (PRD section 11)
common/     envelope, ids, schema registry, config, logging, idempotency
bus/        transport abstraction; swap Redis for anything without touching agents
agents/     one package per agent; av_sentinel verifies all the others
tests/      factories.py holds a valid example message for every topic
```

---

## The two rules worth knowing before you write an agent

**Every message carries a trace.** `trace_id` is minted once by A0 and
propagated unchanged through every downstream message, so any incident can be
replayed back to the original citizen submission. `causation_id` points at the
message that caused this one, which is what rebuilds the decision DAG. Use
`Envelope.originate()` at a trace boundary and `parent.derive()` everywhere
else — both handle this for you.

```python
from common.envelope import Envelope, Producer

understood = ingested.derive(
    topic="reports.understood",
    producer=Producer(agent="A1", version="1.0.0", model="granite-vision-3.2"),
    confidence=0.87,
    rationale="Pothole visible in image; transcript mentions school proximity.",
    payload={...},
)
```

**Delivery is at-least-once, so handlers must be idempotent.** The bus will
redeliver. Key your handler on `envelope.idempotency_key()` and make a repeat a
no-op that returns the prior result (PRD section 9.4). `common.idempotency`
provides the store.

## Adding a topic

1. Add it to `PRODUCERS` in `common/topics.py`.
2. Add `schemas/<topic>.v1.json`, composing the envelope via `allOf` and pinning
   `topic` to a `const`.
3. Add a factory entry in `tests/factories.py`.

The schema tests then enforce the rest — a topic without a schema, a schema
without an envelope reference, or a topic without a factory all fail the build.
