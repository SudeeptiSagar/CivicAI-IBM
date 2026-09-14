# CivicAI — Roadmap and handover

**Status as of 14 September 2026.** Written as a handover: if you are picking this
up cold, read [Start here](#start-here) first, then [What is next](#p4--sentinel-hardening--next-up).

[`PRD.md`](PRD.md) remains the source of truth for requirements. This document
records only what has actually been built, what has not, and what to do next.

---

## At a glance

| | |
|---|---|
| Phases complete | **4 of 8** (P0, P1, P2, P3) |
| PRD milestones met | **M0**, **M1** and **M2** |
| Agents built | **7 of 8** — A0, A1, A2, A3, A4, A5, AV Sentinel |
| Tests | **427 passing** (142 skipped when the stack is down) |
| Next phase | **P4 — Sentinel hardening** |
| Biggest blocker | No IBM watsonx credentials (PRD open question 2) — does not block P4 |

```
P0 ✅  P1 ✅  P2 ✅  P3 ✅  │  P4 ⏭  P5 ⬜  P6 ⬜  P7 ⬜
contracts  infra  core  decide  │  sentinel  pattern  judge  demo
```

---

## Start here

Everything below assumes Docker and Python 3.13. **Python 3.14 is not supported** —
several dependencies have no wheels for it.

```bash
# 1. Environment
py -V:3.13 -m venv .venv                        # Windows
python3.13 -m venv .venv                        # macOS / Linux
.venv/Scripts/python -m pip install -e ".[dev]" # or .venv/bin/python
cp .env.example .env

# 2. Infrastructure
docker compose up -d postgres redis minio minio-init
docker compose run --rm migrate            # database schema
docker compose run --rm load-reference     # city and ward polygons - A0 needs these

# 3. The pipeline
docker compose up -d api agent-sentinel agent-perception agent-dedup agent-synthesis \
    agent-priority agent-routing
curl localhost:8000/v1/health

# 4. Prove it works
docker compose run --rm api python -m scripts.seed_m1 --drain
# expect: 20 reports -> 11 incidents
```

Submit a report and follow it end to end:

```bash
curl -X POST localhost:8000/v1/reports \
  -F device_hash=me -F lat=12.9345 -F lon=77.6101 -F gps_accuracy_m=9 \
  -F text="Huge pothole outside the school gate"

curl localhost:8000/v1/reports/<report_id>   # status + "N others reported this"
curl localhost:8000/v1/trace/<trace_id>      # every agent decision + Sentinel verdicts
```

### Before every commit

All four must pass. CI runs exactly these.

```bash
ruff check .
ruff format --check .
mypy
pytest
```

Integration tests **skip** when the stack is down, so `pytest` stays green on a
laptop with nothing running — and a skip is reported as a skip, never as a pass.
With the stack up they use a separate `civicai_test` database, Redis index 15 and
a `tests/` key prefix in the bucket, so they never touch your dev data.

---

## Completed phases

### P0 — Contracts and foundation ✅

`0625ff9` · originally on `feat/p0-contracts` · 50 files, +4,647

The layer every agent depends on. Runs with no infrastructure at all, which is
what let CI verify it before Docker was available.

- **`schemas/`** — the envelope plus 15 topic schemas and 3 patterned families
  (`*.skipped`, `quarantine.*`, `control.*`). This is the contract source of
  truth (PRD §11), not the Python models.
- **`common/`** — envelope with `trace_id` propagation and `causation_id`
  chaining, UUIDv7 minting, schema registry resolving by major version,
  settings, JSON logging with trace correlation, the PRD §9.4 idempotency key.
- **`bus/`** — the `Bus` interface and an in-memory implementation.
- **Sentinel L1** — structural verification against the registered contract for
  `(topic, schema_version)`. Two verdicts only: `pass` or `fail_hard`.

Drift tests fail the build if `common/envelope.py` and `schemas/envelope.v1.json`
disagree, and a table of 32 known-bad payloads asserts what L1 must reject.

### P1 — Infrastructure and persistence ✅

`23db8c0` · originally on `feat/p1-infrastructure` · 34 files, +4,752

**PRD milestone M0 met:** an event flows A0 → echo → Sentinel L1 and is visible
in the trace viewer.

- **`docker-compose.yml`** — Postgres 16 + PostGIS 3.4 + pgvector 0.8 in one
  custom image (no published image carries both), Redis, MinIO, the API and one
  container per agent.
- **`db/`** — a migration runner over numbered `.sql` files with a checksum
  guard that refuses to ignore an edited migration. Full PRD §10 schema plus six
  documented additions (see [`docs/data-model.md`](docs/data-model.md)).
- **`bus/redis.py`** — Redis Streams. Redelivery works differently from the
  in-memory double (Redis has no requeue), so a parity test asserts that
  agent-visible behaviour is identical.
- **`agents/base.py`** — the PRD §7 agent contract implemented once instead of
  eight times: consumer groups, durable idempotency, the three-attempt retry
  budget, skip events, deadlettering, archive-before-publish, run telemetry.
- **`api/`** — `/v1/health`, `/v1/health/agents`, `/v1/trace/{trace_id}`.

### P2 — The core path ✅

`0ba6679` · originally on `feat/p2-core-path` · 34 files, +5,275

**PRD milestone M1 met:** 20 seeded reports collapse into exactly the 11 expected
incidents. Four citizens submitting in parallel produce one incident with four
reports from four devices.

- **A0 Intake** (`agents/a0_intake/`) — the trace boundary, driven by
  `POST /v1/reports` rather than the bus. Media validation with content sniffing,
  EXIF stripping by re-encode, PostGIS ward join, city-boundary rejection,
  per-device rate limiting that counts rejected attempts too. A rejection
  publishes `reports.rejected`, so it is auditable.
- **A1 Perception** (`agents/a1_perception/`) — extraction plus a 768-dimension
  embedding. Degrades openly: a modality with no provider is recorded as unread,
  lowers confidence and is named in the rationale.
- **A2 Dedup** (`agents/a2_dedup/`, `common/matching.py`) — four-component
  scoring over PostGIS and pgvector with the PRD's thresholds, radii and windows.
  Never merges across categories.
- **A3 Synthesis** (`agents/a3_synthesis/`) — consolidation with a
  GPS-accuracy-weighted centroid, recomputed from members so `report_count`
  cannot drift. Pattern mode is **not** here; it is P5.
- **`common/llm/`** — the provider interface PRD §6.2 requires, plus a
  deterministic lexical baseline. See [the honest caveats](#1-the-reasoning-provider-is-not-a-model).
- **API** — `POST /v1/reports`, `GET /v1/reports/{id}`.

### P3 — Decisioning ✅

`pending commit` · on worktree branch `worktree-agent-af9d1af2a7017f207` · 13 files

**PRD milestone M2 met:** every seeded incident gets an explainable priority
score and a department. Purely deterministic arithmetic — the missing watsonx
credentials do not block this phase, and nothing here calls a model.

- **A4 Prioritization** (`agents/a4_priority/`) — six-factor weighted score in
  [0,100] (hazard severity 0.30, exposure 0.20, vulnerable-site proximity 0.15,
  corroboration 0.15, age unresolved 0.10, velocity 0.10), every score carrying
  its full `factor_breakdown` and a traceable one-sentence `why`. Corroboration
  is log-scaled against `distinct_reporters` (already device-deduplicated by
  A3), so one street cannot brigade the queue. A life-safety hazard flag (open
  manhole, live wire, collapsed structure, gas leak) floors the score at 85
  regardless of the weighted sum, with `life_safety_floor_applied` recording
  that the floor, not the arithmetic, decided the number. The pure scoring
  functions are importable and DB-free, mirroring `common/matching.py`'s
  discipline.
- **A5 Routing** (`agents/a5_routing/`) — a category→department map (13 of 14
  taxonomy categories; `"other"` is deliberately unmapped), one
  ambiguous-ownership rule (`waterlogging`/`drain_overflow` cc
  `roads_and_infrastructure` — standing water is drainage's fix but a road
  problem too), and an SLA clock that scales each department's
  `default_sla_hours` by a priority-band multiplier (critical ×0.25 through
  low ×1.5). An unmapped or unregistered department routes to
  `incidents.unrouted` with a reason code — never a silent default.
- **`db/migrations/0003_priority_routing_reference.sql`** — `poi` (schools/
  hospitals, for the proximity factor) and `ward_road_class` (a coarse
  per-ward proxy for the exposure factor). Seeded from
  `data/reference/bengaluru_poi.geojson`, loaded by the extended
  `scripts/load_reference_data.py` / `load-reference` compose step.
- **API** — `GET /v1/incidents` (filter by ward/department/band/status, sorted
  by priority) and `GET /v1/incidents/{id}`.
- **`docker-compose.yml`** — `agent-priority` and `agent-routing` service
  blocks, following the `agent-synthesis` anchor pattern.

**Honest caveats specific to this phase**, also recorded in the code:

- The `poi` seed is a **small, synthetic, hand-placed fixture** — *not* a real
  BBMP/OSM extract. It exists so `vulnerable_site_proximity` has something real
  to compute a PostGIS distance against, not to claim real school/hospital
  coverage.
- `exposure` uses a **ward-level road-class proxy**
  (`ward_road_class`), not a real road-segment network join. A real network
  would classify the incident's actual street; this classifies its whole ward.
- **No ward→office jurisdiction table exists.** `office_id` on
  `incidents.routed` is always `null` — `ward_id` is the real jurisdiction
  signal this phase has.

---

## Things that are true of this build

Three caveats a reader could otherwise mistake. All three are recorded in the
code and in `docs/`, not only here.

### 1. The reasoning provider is not a model

There are no IBM watsonx credentials (PRD open question 2).
`get_provider("watsonx")` **raises** rather than returning a stub.

What runs instead is `common/llm/heuristic.py`: keyword matching for category
and hazard flags, small rules for severity, and a hashed character-n-gram vector
for `embed()`. It exists so the core path is runnable and measurable without
credentials, and so a real provider has a floor to beat. It caps its own
confidence at 0.55.

**Its embedding is lexical, not semantic.** On the M1 fixture, genuine duplicates
score spatial 0.79–0.95 and temporal 0.78–0.99 (strong, correct) but semantic
only 0.39–0.67, where a real embedding would reach 0.85+. Most true duplicates
therefore land in the 0.60–0.82 grey zone instead of auto-linking.

**Configuring a real provider is the single highest-value change available.**
Full detail and measured numbers: [`docs/perception-and-dedup.md`](docs/perception-and-dedup.md).

### 2. Faces and plates are not blurred

PRD §15 asks for it. It needs a detector this build does not have. EXIF
stripping *is* implemented and verified against the stored bytes, but **a photo
containing a bystander is stored with that person legible.**

This is an open PII gap, not an oversight. Treat it as a blocker for any
deployment handling real citizen photographs.
[`docs/media-handling.md`](docs/media-handling.md).

### 3. Sentinel verifies alongside consumers, not in front of them

PRD §8 calls for downstream agents to act only on events carrying a valid
verdict. Today Sentinel and each agent hold independent consumer groups on the
same topic, so an agent can begin work on a message Sentinel is about to
quarantine.

`/v1/health/agents` reports this honestly as `sentinel_gate_enforced: false`
rather than letting the configured mode imply a guarantee. Closing it is P4.
[`docs/sentinel.md`](docs/sentinel.md).

---

## Remaining phases

### P4 — Sentinel hardening — NEXT UP

**PRD milestone M3.** Acceptance: a deliberately broken agent build is blocked by CI.

- `layers/invariants.py` — L2 business rules, the full per-agent table from PRD §8.1
- `rules/` — per-agent invariant definitions (A0 through A7)
- **The verdict gate** — consumers wait for a verdict before acting, with the
  strict / permissive / shadow modes and the 2-second deadline from PRD §8.3.
  This closes caveat 3 above.
- `fail_soft` — return the event to the producing agent for one retry with the
  failure reason appended
- `goldens/` — roughly 50 hand-labelled reports and 15 known duplicate clusters
- CI regression gate — replay the goldens on every deploy, block on regression
  against the recorded baseline
- Quarantine triage — a surface for reviewing and releasing blocked envelopes

### P5 — Pattern detection and closure

**PRD milestone M4.** Acceptance: the drainage SuperIncident appears; one
incident is closed and verified end to end.

- **A3 pattern mode** — windowed ward × 48 h search for co-occurring categories
- Seed rules: waterlogging + drain overflow + sewage → drainage failure;
  repeated potholes + waterlogging on one stretch → subsurface road failure;
  multiple streetlight failures on one feeder → electrical feeder fault
- **SuperIncidents** — advisory only. They link incidents and never delete or
  override one.
- `agents/a6_resolution/` — consumes `resolution.claimed`, notifies the
  incident's reporters, requests after-photos
- Before/after comparison — same-location check then visual comparison →
  `fixed` / `unchanged` / **`inconclusive`** (the agent is allowed to say it
  does not know)
- Closure rules: ≥2 citizen confirmations **or** a confident visual match. Any
  dispute reopens the incident, escalates the priority band and restarts the SLA.
- API: `GET /v1/super-incidents`, `POST /v1/incidents/{id}/resolve`,
  `POST /v1/incidents/{id}/confirm`

### P6 — Judge and evidence

**PRD milestone M5.** Acceptance: judge verdicts on every event in the demo run.

Most credential-dependent phase.

- `layers/judge.py` — L3 LLM-as-judge on a rubric, using a **different model and
  config from the acting agent**; never self-grading
- `layers/continuous.py` — L4: golden replay per deploy, hourly canary
  injection, drift monitors at >2σ, loop and thrash detection
- `sentinel.alert` emission and an ops surface
- Sentinel meta-check — known-bad fixtures replayed each run; self-alert and
  degrade to `strict` if it passes something it should have failed
- `agents/a7_evidence/` — sample-footage mode over `data/sample_cctv/`;
  `corroborating` / `contradicting` / `unavailable`
- Hard constraint: evidence may raise confidence or flag a contradiction, and
  may **never** auto-close an incident

### P7 — Surfaces and demo

**PRD milestone M6.** Acceptance: the five-minute demo runs clean twice in a row.

**No frontend code exists yet — `web/` is not in the repository.**

- `web/citizen/` — React PWA: camera, microphone, automatic GPS, submission in
  under 30 seconds
- "17 others reported this" — the duplicate-aware confirmation, already served
  by `GET /v1/reports/{id}`
- `web/dashboard/` — department queue ranked by priority, with the evidence
  bundle and the reason behind each score
- Admin view — cross-department map, hotspots, emerging patterns, SLA breaches
- Trace viewer UI over `/v1/trace/{id}` (currently JSON only)
- `docs/demo-script.md` and the seeded five-minute story from PRD §17

---

## Phase to PRD milestone

| Phase | PRD milestone | Status | Commit |
|---|---|---|---|
| P0 | M0 — Skeleton (part) | Complete | `0625ff9` |
| P1 | M0 — Skeleton (complete) | Complete | `23db8c0` |
| P2 | M1 — Core path | Complete | `0ba6679` |
| P3 | M2 — Decisioning | Complete | `pending commit` |
| P4 | M3 — Sentinel | Next | — |
| P5 | M4 — Pattern + closure | Remaining | — |
| P6 | M5 — Judge + evidence | Remaining | — |
| P7 | M6 — Demo polish | Remaining | — |

---

## Work that can run in parallel

Five lanes that do not touch the files P4 will change. Each names the paths it
owns, so two people can work without stepping on each other.

### Lane 1 — Citizen PWA and dashboards
**Owns:** `web/citizen/`, `web/dashboard/`

The largest parallel piece of remaining work and the one a judge sees first.
`web/` does not exist, so there is nothing to collide with. Build against the
endpoints that are already live, including `GET /v1/incidents` and
`GET /v1/incidents/{id}` (P3, landed).

*No conflict with any Python work. Can start immediately.*

### Lane 2 — Sentinel L2 invariants
**Owns:** `agents/av_sentinel/layers/invariants.py`, `agents/av_sentinel/rules/`

The A0–A3 invariants from PRD §8.1 can be written now: those agents exist and
their contracts are frozen. Work rule-by-rule against the schemas rather than
the agents, and the A4–A7 rules drop in later without rework.

*Coordinate: the verdict gate touches `agents/base.py` — leave that to the P4 owner.*

### Lane 3 — Golden set and eval harness
**Owns:** `data/goldens/`, `scripts/evaluate.py`

Roughly 50 labelled reports and 15 known duplicate clusters, plus a scorer for
the PRD §14 metrics: dedup recall, false-merge rate, routing accuracy,
compression ratio. P4 needs this, and it is the only honest way to measure
whether a real model beats the current lexical baseline.

**Include Kannada and Hindi reports** — the current fixture deliberately has
none, because the baseline cannot classify them (PRD open question 3).

*No conflict. Can start immediately.*

### Lane 4 — The watsonx provider
**Owns:** `common/llm/watsonx.py`

One new file behind an interface that already exists and is already tested. It
unblocks more than anything else here: real semantic dedup, ASR, vision, LLM
adjudication and the L3 judge all depend on it.

*Blocked on watsonx credentials and model availability (PRD open question 2).*

### Lane 5 — Real ward boundaries
**Owns:** `data/reference/bengaluru.geojson`

The bundled polygons are hand-drawn rectangles around Koramangala, not BBMP
boundaries. Swapping in real geometry is a data change with no code change — the
loader and the `source` column already handle provenance. Answering PRD open
question 1 belongs here.

*Needs a source for BBMP ward boundaries.*

---

## Coordination hazards

| Hazard | Why it bites | How to avoid it |
|---|---|---|
| `db/migrations/` | Two people both write the same numbered file; the runner's checksum guard rejects the loser | Claim the next number before writing the file. P3 used `0003`. **P4 owns `0004`.** |
| `api/main.py` | One module holds every route; P3 added `/v1/incidents`, P5 will add more | Split into `api/routes/` before two people touch it, or sequence the edits |
| `agents/base.py` | P4's verdict gate changes the handler path every agent runs through | Land the gate on its own branch and rebase agent work onto it |
| `common/topics.py` + `schemas/` | A new topic needs a registry entry, a schema and a factory; tests fail on any one missing | All three in one commit. The README's "Adding a topic" section is the checklist. |
| `docker-compose.yml` | Every phase appends agent services to the same file | Append only, one service block per agent; conflicts stay trivial |

---

## Open debt

| Gap | Impact | Closes in |
|---|---|---|
| Sentinel does not gate consumers | An agent can act on a message Sentinel is about to quarantine | P4 |
| No model — lexical baseline only | Semantic dedup caps around 0.67; most true duplicates land in the grey zone | Lane 4 |
| No face or plate blurring | **Open PII gap.** A photo containing a bystander is stored with them legible | P6 |
| No ASR, no vision | Audio and photos are stored but never read | P6 |
| No reverse geocoding | Reports carry coordinates and a ward, not a street address | Unscheduled |
| No landmark-based location refinement | The landmark is extracted and surfaced; turning it into coordinates needs a geocoder | Unscheduled |
| Ward polygons are fixtures | Hand-drawn rectangles, not BBMP boundaries | Lane 5 |
| No media retention policy | Blobs accumulate indefinitely; needs a policy decision the PRD does not make | Undecided |
| POI reference data is synthetic | `poi` (schools/hospitals) is a small hand-placed fixture, not a real BBMP/OSM extract; A4's proximity factor is a real distance computation against fake points | Unscheduled |
| Exposure is a ward-level proxy | `ward_road_class` is one dominant class per ward, not a real road-segment network join; a road half a ward away scores the same exposure | Unscheduled |
| No ward→office jurisdiction table | A5's `office_id` is always `null`; `ward_id` is the only jurisdiction signal routing has | Unscheduled |

### PRD open questions still unanswered

1. Real BBMP complaint feed, or fully synthetic seed data for the demo?
2. Which watsonx models are available on the track, and what are the rate limits?
3. Kannada ASR quality — is a fallback to text entry needed?
4. Does the demo need multi-ward scale, or is one ward with a rich story stronger?
5. Show the Sentinel trace viewer to judges as a first-class feature?
   *(The PRD recommends yes. The data is already there; only the UI is missing.)*

---

## Conventions worth knowing before you write code

**Every message carries a trace.** `trace_id` is minted once by A0 and
propagated unchanged; `causation_id` points at the message that caused this one.
`Envelope.originate()` and `parent.derive()` handle both — use them rather than
constructing envelopes by hand.

**Delivery is at-least-once, so handlers must be idempotent.** The runtime keys
on `envelope.idempotency_key()` and records the outcome in `handler_results`,
making a repeat a no-op that returns the prior result (PRD §9.4).

**Whoever publishes, archives.** `common.messagelog.archive()` before
`bus.publish()`, always in that order — a message a consumer can see must
already be in the audit trail. `Agent.emit()` does this for you.

**Never invent what you could not read.** If a modality has no provider, record
the gap, lower the confidence and say so in the rationale — or raise
`SkipSignal`. An agent that emits a plausible default is indistinguishable from
one that actually looked, and nothing downstream can tell the difference.

### Further reading

- [`docs/perception-and-dedup.md`](docs/perception-and-dedup.md) — how A1, A2 and
  A3 actually behave, with measured numbers
- [`docs/message-contracts.md`](docs/message-contracts.md) — the envelope, topic
  families, and decisions made where the PRD was silent
- [`docs/data-model.md`](docs/data-model.md) — the schema and the tables added
  beyond PRD §10
- [`docs/sentinel.md`](docs/sentinel.md) — what verification runs today and what
  does not
- [`docs/media-handling.md`](docs/media-handling.md) — which PRD §15 privacy
  controls exist
