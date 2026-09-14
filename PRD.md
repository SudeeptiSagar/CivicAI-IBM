# CivicAI — Product Requirements Document

**Repo:** https://github.com/SudeeptiSagar/CivicAI-IBM
**Status:** Draft v1.0
**Owner:** CivicAI team
**Last updated:** 2026-09-14

---

## 1. One-line definition

> **CivicAI transforms thousands of fragmented citizen complaints into a smaller set of verified, prioritized civic incidents, helping authorities understand what is actually happening in the city and what needs attention first.**

In plain words: people tell us what's wrong; the system figures out which complaints are about the *same* problem, how serious that problem is, where it is, and who needs to fix it.

---

## 2. Problem statement

A city complaint portal receives reports one at a time and stores them one at a time. A single pothole outside a school can generate 17 separate tickets across 5 days. The authority sees 17 rows, not one dangerous pothole.

Three failures follow:

1. **Volume without signal.** Duplicate reports inflate queues and hide the small number of genuinely severe problems.
2. **No cross-report reasoning.** Waterlogging, drain overflow, sewage smell and a traffic jam in the same 300 m over the same 48 h are logged as four unrelated categories, when they are one drainage failure.
3. **No closure.** "Resolved" is a status field set by the department, not a verified fact. Citizens have no evidence the problem is actually gone.

## 3. What CivicAI is *not*

- Not a replacement for BBMP's (or any municipality's) complaint system. It is a **middle layer** that consumes reports and emits incidents.
- Not "an AI pothole detector." Detection is one small step inside one agent.
- Not "an AI complaint management system." The value is aggregation, prioritization and explanation, not ticket CRUD.
- Not CCTV-dependent. CCTV is an optional confidence booster (§10), never a hard dependency.

## 4. Goals and non-goals

### Goals (v1)
| # | Goal | Measure |
|---|------|---------|
| G1 | Collapse duplicate reports into incidents | ≥ 80% dedup recall, ≤ 5% false-merge rate on the eval set |
| G2 | Produce an explainable priority score per incident | Every score ships with a factor breakdown; no unexplained scores |
| G3 | Detect multi-category patterns (root-cause hypotheses) | ≥ 1 correct hypothesis surfaced on the seeded drainage scenario |
| G4 | Route each incident to the correct department | ≥ 90% routing accuracy on labelled set |
| G5 | Verify resolution with citizen evidence | Before/after comparison on ≥ 1 closed incident end-to-end |
| G6 | Every agent output is independently verified | 100% of inter-agent messages pass Sentinel (§8) before downstream consumption |

### Non-goals (v1)
- Live municipal CCTV integration.
- Mobile app store release (PWA is sufficient).
- Field-worker task assignment / workforce scheduling.
- Multi-city tenancy (single city, Bengaluru, hardcoded boundaries).

## 5. Personas

| Persona | Needs | Primary surface |
|---------|-------|-----------------|
| **Citizen** (Priya, commuter) | Report in < 30 s, no forms, know if it's already reported, see progress | PWA: camera + mic + auto-GPS |
| **Department officer** (Roads & Infra) | A ranked queue of *incidents*, not tickets; evidence bundle; why it's urgent | Department dashboard |
| **City administrator** | Cross-department view, hotspots, emerging patterns, SLA breaches | City dashboard / map |
| **Auditor (internal)** | Trace any incident back to its constituent reports and agent decisions | Trace viewer (read-only) |

### Core user stories
- As a citizen, I take a photo, speak one sentence, and my report is filed with location, category and severity extracted automatically.
- As a citizen, I am told "17 others reported this; it is incident #1042" instead of getting a new orphan ticket.
- As an officer, I open my dashboard and see incidents ranked 93 / 81 / 64, each with a reason.
- As an officer, I mark an incident resolved and the system asks the reporters to confirm.
- As an admin, I see "possible underlying drainage problem in Koramangala 5th Block" derived from five different complaint categories.

---

## 6. System architecture

### 6.1 Shape

CivicAI is an **event-driven multi-agent system**. Agents are stateless workers; all durable truth lives in Postgres. Agents never call each other directly — they publish and subscribe on a message bus. This gives replayability, independent scaling, and a single choke point where the verification agent can inspect everything.

```text
                    ┌──────────────────────────────────────────┐
 Citizen PWA ─────► │ A0 Intake / Gateway                      │
 (photo, voice,     └──────────────┬───────────────────────────┘
  text, GPS)                       │ reports.ingested
                                   ▼
                    ┌──────────────────────────────────────────┐
                    │ A1 Perception Agent (multimodal)         │
                    └──────────────┬───────────────────────────┘
                                   │ reports.understood
                                   ▼
                    ┌──────────────────────────────────────────┐
                    │ A2 Deduplication Agent (geo+vec+time)    │
                    └──────────────┬───────────────────────────┘
                                   │ reports.linked
                                   ▼
                    ┌──────────────────────────────────────────┐
                    │ A3 Incident Synthesis Agent              │
                    └──────────────┬───────────────────────────┘
                                   │ incidents.updated
                        ┌──────────┴──────────┐
                        ▼                     ▼
        ┌───────────────────────┐  ┌──────────────────────────┐
        │ A4 Prioritization     │  │ A7 Evidence Agent (opt.) │
        └───────────┬───────────┘  └──────────┬───────────────┘
                    │ incidents.prioritized   │ evidence.attached
                    ▼                         │
        ┌───────────────────────┐             │
        │ A5 Routing Agent      │◄────────────┘
        └───────────┬───────────┘
                    │ incidents.routed
                    ▼
        ┌───────────────────────┐   resolution.claimed / .verified
        │ A6 Resolution Agent   │◄──────────────► Department dashboard
        └───────────────────────┘

   ╔══════════════════════════════════════════════════════════════╗
   ║ AV  SENTINEL — subscribes to EVERY topic, verifies EVERY     ║
   ║     agent output, emits verification.results / quarantine    ║
   ╚══════════════════════════════════════════════════════════════╝
```

### 6.2 Stack

| Layer | Choice | Note |
|-------|--------|------|
| Bus | Redis Streams (consumer groups) | Kafka-compatible semantics without Kafka ops cost; swap later behind `bus/` interface |
| State | PostgreSQL 16 + PostGIS + pgvector | Geo queries and embedding search in one store |
| Object store | S3-compatible (MinIO locally) | Photos, audio, before/after pairs |
| Reasoning | Pluggable LLM provider behind `llm/provider.py` | IBM watsonx (Granite) as primary given the IBM track; OpenAI/Anthropic/local as fallbacks. **No agent imports a vendor SDK directly.** |
| Vision | Provider vision endpoint + optional local detector for offline demo | |
| ASR | Whisper-class model, Kannada/Hindi/English | |
| API | FastAPI | |
| Frontend | React PWA (citizen) + React dashboard (department/admin) | |
| Orchestration | docker-compose (dev), single-node k8s optional | |

---

## 7. Agent roster

Every agent obeys the same contract:

- consumes from exactly one input topic (plus `control.*`),
- is **idempotent** on `report_id` / `incident_id` + `schema_version`,
- emits exactly one output event per input event (or a `*.skipped` event with a reason),
- attaches a `confidence` in `[0,1]` and a machine-readable `rationale`,
- never writes to a table owned by another agent,
- fails loudly to `deadletter` rather than guessing.

### A0 — Intake / Gateway Agent
**Job:** turn a raw citizen submission into a well-formed `Report`.
**In:** HTTP multipart from PWA. **Out:** `reports.ingested`.
- Validates media type/size, strips EXIF except geotag, stores blobs, mints `report_id`.
- Resolves location: device GPS → reverse geocode → ward/zone lookup (PostGIS polygon join).
- Rate-limits per device to blunt spam/brigading.
- Emits `reports.rejected` with a reason on validation failure.

### A1 — Perception Agent (multimodal understanding)
**Job:** "What is this person reporting?"
**In:** `reports.ingested`. **Out:** `reports.understood`.
- ASR on audio → transcript (with detected language).
- Vision on image → objects, scene, damage estimate.
- Fuses transcript + vision + free text into a structured extraction:
  `category`, `subcategory`, `severity_raw (1-5)`, `hazard_flags[]`, `landmark_text`, `summary (≤ 20 words)`, `embedding (768-d)`.
- Refines location using landmark text when GPS accuracy > 50 m.
- **Guardrail:** if the image and the text disagree (photo of garbage, text says "streetlight"), lower confidence and set `modality_conflict = true` rather than picking one.

### A2 — Deduplication Agent
**Job:** "Has someone already reported this?"
**In:** `reports.understood`. **Out:** `reports.linked`.
Candidate generation, then scoring:
1. **Spatial:** PostGIS `ST_DWithin` within a category-specific radius (pothole 75 m, garbage 150 m, waterlogging 300 m).
2. **Temporal:** within a category-specific window (pothole 21 d, waterlogging 3 d, garbage 7 d).
3. **Semantic:** cosine similarity of A1 embeddings.
4. **Visual:** perceptual hash + image embedding similarity when both reports have photos.

`match_score = w_s·spatial + w_t·temporal + w_e·semantic + w_v·visual`
- `≥ 0.82` → auto-link to existing incident.
- `0.60 – 0.82` → emit as `candidate_link`, A3 arbitrates with an LLM adjudication call.
- `< 0.60` → new incident seed.

**Never silently merges across categories** — cross-category association is A3's job, not A2's.

### A3 — Incident Synthesis Agent
**Job:** "Are all these reports actually one problem? And is there a bigger problem behind them?"
**In:** `reports.linked`. **Out:** `incidents.updated`.
Two modes:
- **Consolidation:** create/update the `Incident` record — canonical title, canonical location (weighted centroid of member reports, weighted by GPS accuracy), first-reported time, report count, evidence bundle.
- **Pattern detection (the differentiator):** a windowed job over ward × 48 h looking for *co-occurring categories* that imply a shared cause. Ships a `root_cause_hypothesis` with member incidents and a confidence.
  - Seed rules (waterlogging + drain overflow + sewage → drainage failure; repeated potholes + waterlogging on one stretch → subsurface/drainage-driven road failure; multiple streetlight failures on one feeder → electrical feeder fault), then LLM-generated hypotheses over the co-occurrence matrix.
  - Hypotheses are **advisory**: they create a `SuperIncident` that links incidents, never deletes or overrides them.

### A4 — Prioritization Agent
**Job:** "Which problem needs attention first?"
**In:** `incidents.updated`. **Out:** `incidents.prioritized`.

Deterministic, auditable score in `[0,100]`:

| Factor | Weight | Source |
|--------|--------|--------|
| Hazard severity | 0.30 | A1 severity + hazard flags |
| Exposure (road class, footfall) | 0.20 | OSM road class, POI density |
| Vulnerable-site proximity (school/hospital) | 0.15 | PostGIS distance to POI layer |
| Corroboration (distinct reporters) | 0.15 | distinct devices, log-scaled, capped |
| Age unresolved | 0.10 | days since first report |
| Velocity (report rate increasing) | 0.10 | reports in last 24 h vs trailing mean |

Rules on top of the weighted sum:
- Hard floor 85 for life-safety flags (open manhole, live wire, collapsed structure) regardless of report count.
- Corroboration is **log-scaled and device-deduplicated** so a single street can't brigade the queue.
- Output includes `factor_breakdown[]` and a one-sentence natural-language `why` — a score with no breakdown is rejected by Sentinel.

### A5 — Routing Agent
**Job:** "Which department owns this?"
**In:** `incidents.prioritized`. **Out:** `incidents.routed`.
- Category → department map (Roads & Infrastructure, Drainage/Water, Solid Waste Management, Electrical/Streetlights, Health, Parks).
- Ward → jurisdiction lookup for the specific office.
- SLA clock assignment by (department, priority band).
- Ambiguous cases (e.g. road collapse caused by a leaking main) emit `primary_department` + `cc_departments[]` rather than forcing a single owner.
- Unknown category → `incidents.unrouted` for human triage, never a silent default.

### A6 — Resolution & Closure Agent
**Job:** close the loop.
**In:** `resolution.claimed` (from dashboard), citizen after-photos. **Out:** `resolution.verified` / `resolution.disputed`.
- On claim, notifies the incident's reporters and requests an after-photo.
- Before/after comparison: same-location check, then visual comparison of the hazard region → `fixed` / `unchanged` / `inconclusive`.
- ≥ 2 citizen confirmations **or** a confident visual match → `resolution.verified`.
- Any citizen dispute or `unchanged` verdict → reopen, escalate priority band, restart SLA.
- Explicit `inconclusive` state — the agent is allowed to say it doesn't know.

### A7 — Evidence Agent (optional layer)
**Job:** raise confidence with non-citizen evidence.
**In:** `incidents.updated`. **Out:** `evidence.attached`.
- Given an incident location/time, query available camera sources; run the same perception pipeline over sampled frames; attach `corroborating` / `contradicting` / `unavailable`.
- **Demo runs against a sample-footage directory, not live municipal feeds.** Feed access is a procurement dependency and is explicitly out of scope for v1.
- Evidence can only *raise* confidence or flag a contradiction for human review; it can never auto-close an incident.

---

## 8. AV — Sentinel: the verification agent

A dedicated agent whose only job is to check every other agent's work. It subscribes to **every** topic and sits between producers and consumers logically: downstream agents only act on events carrying a valid Sentinel verdict, or on events whose verification deadline has lapsed in `permissive` mode.

### 8.1 Four verification layers

**L1 — Structural.** Envelope + payload validated against the JSON Schema for `(topic, schema_version)`. Required fields, enums, ranges, `confidence ∈ [0,1]`, non-null IDs. Fail → hard reject.

**L2 — Invariant / business rules.** Per-agent assertions:

| Agent | Sample invariants |
|-------|-------------------|
| A0 | Location inside city polygon; media present for `has_photo=true`; `report_id` unused |
| A1 | Category ∈ taxonomy; summary ≤ 20 words; embedding dims = 768 and non-zero; `modality_conflict` set whenever vision/text categories differ |
| A2 | An incident's members share a category; every member within max radius of centroid; no report in two incidents; merge count monotonic |
| A3 | `report_count` = actual member count; centroid recomputed; a SuperIncident never removes a member incident |
| A4 | Score ∈ [0,100]; breakdown weights sum to 1.0 ± 0.01; recomputed score matches emitted score; life-safety floor honoured; score monotonic w.r.t. added corroboration |
| A5 | Department ∈ registry; ward jurisdiction valid; SLA set; unknown → unrouted, never defaulted |
| A6 | No `verified` without ≥ 2 confirmations or a confident visual match; disputed always reopens |
| A7 | Evidence never flips an incident to resolved; source URI recorded |

**L3 — Semantic (LLM-as-judge).** Sampled at 100% for the demo, ~10% + all low-confidence events in production. An independent model, prompted with the original inputs and the agent's output, scores faithfulness on a rubric:
- A1: does the extraction match what the photo and transcript actually show?
- A2/A3: are these reports plausibly the same real-world problem? (adjudicates the 0.60–0.82 grey zone)
- A4: does the `why` sentence actually follow from the factor breakdown?
- A5: is this the department a city officer would pick?

Judge output: `verdict ∈ {pass, warn, fail}` + reason. The judge is a *different* model/config from the acting agent — never self-grading.

**L4 — Continuous / systemic.**
- **Golden set:** ~50 hand-labelled reports and ~15 known duplicate clusters replayed on every deploy; regression vs. the recorded baseline blocks the release.
- **Canary injection:** synthetic reports with known ground truth injected hourly into the live stream; each agent's handling is scored end-to-end.
- **Drift monitors:** category distribution, mean confidence, dedup rate, mean priority. A >2σ shift raises `sentinel.alert`.
- **Loop/thrash detection:** an incident re-prioritized or re-routed more than N times in a window is frozen for human review.

### 8.2 Verdicts and behaviour

| Verdict | Effect |
|---------|--------|
| `pass` | Event is marked verified; downstream agents proceed |
| `warn` | Event proceeds; flagged in the trace viewer; counted in drift metrics |
| `fail_soft` | Event is returned to the producing agent for one retry with the failure reason appended |
| `fail_hard` | Event quarantined to `quarantine.<topic>`; incident flagged `needs_human_review`; downstream never sees it |

Sentinel is itself verified by a small **meta-check**: fixed fixtures with known-bad payloads are replayed each run; if Sentinel passes a payload it must fail, it self-alerts and degrades to `strict` mode (everything that isn't explicitly verified is quarantined).

Sentinel **cannot mutate business data**. It writes only to `verification_results`, `quarantine`, and `sentinel_alerts`. It can flag and block; it cannot fix.

### 8.3 Operating modes

- `strict` — nothing proceeds without an explicit `pass`. Used in CI and demo.
- `permissive` — events proceed if no verdict arrives within the deadline (default 2 s); the gap is logged. Used under load.
- `shadow` — verdicts recorded, nothing blocked. Used when calibrating new rules.

---

## 9. Inter-agent communication

### 9.1 Transport

Redis Streams, one stream per topic, one consumer group per agent. At-least-once delivery, consumer-side idempotency, `XAUTOCLAIM` for stalled consumers. Every agent talks to the bus through `bus/` (`publish`, `subscribe`, `ack`, `nack`) so the transport can be swapped for Kafka/RabbitMQ without touching agent code.

### 9.2 Envelope (all messages)

```json
{
  "message_id": "uuid-v7",
  "trace_id": "uuid-v7",
  "causation_id": "uuid-v7 of the message that caused this one",
  "correlation_id": "report_id or incident_id",
  "topic": "reports.understood",
  "schema_version": "1.0.0",
  "emitted_at": "2026-09-14T07:21:33Z",
  "producer": { "agent": "A1", "version": "1.2.0", "model": "granite-vision-3.2" },
  "confidence": 0.87,
  "rationale": "Pothole visible in image; transcript mentions school proximity.",
  "verification": {
    "status": "pending | pass | warn | fail_soft | fail_hard",
    "verdict_id": null,
    "checked_layers": []
  },
  "payload": { }
}
```

Rules:
- `trace_id` is minted by A0 and **propagated unchanged** through every downstream message, so any incident can be replayed back to the original citizen submission.
- `causation_id` builds the exact DAG of decisions for the trace viewer.
- Payloads are additive-only within a major `schema_version`; breaking changes bump major and run both consumers side by side during migration.

### 9.3 Topics

| Topic | Producer | Consumers |
|-------|----------|-----------|
| `reports.ingested` | A0 | A1, AV |
| `reports.rejected` | A0 | AV, admin UI |
| `reports.understood` | A1 | A2, AV |
| `reports.linked` | A2 | A3, AV |
| `incidents.updated` | A3 | A4, A7, AV |
| `incidents.prioritized` | A4 | A5, AV |
| `incidents.routed` | A5 | dashboards, A6, AV |
| `incidents.unrouted` | A5 | admin UI, AV |
| `evidence.attached` | A7 | A3, A4, AV |
| `resolution.claimed` | dashboard | A6, AV |
| `resolution.verified` / `.disputed` | A6 | dashboards, A4 (reopen), AV |
| `verification.results` | AV | all agents, trace viewer |
| `sentinel.alert` | AV | admin UI, ops |
| `quarantine.<topic>` | AV | human triage |
| `deadletter` | any | ops |
| `control.<agent>` | ops | target agent (pause, reload config, replay) |

### 9.4 Delivery guarantees

- **At-least-once + idempotent handlers.** Handler key = `(correlation_id, topic, schema_version, producer.version)`; a repeat is a no-op returning the prior result.
- **Retries:** 3 attempts, exponential backoff (1s, 4s, 16s), then `deadletter` with the full envelope and error chain.
- **Ordering:** guaranteed per `correlation_id` only. Agents must not assume global order.
- **Poison-pill protection:** an event that fails 3× in the same agent is quarantined, not endlessly re-queued.
- **Replay:** any stream range can be replayed into a shadow consumer group for debugging or golden-set regression.

---

## 10. Data model (core tables)

```sql
reports(report_id PK, trace_id, device_hash, created_at, geom GEOGRAPHY(POINT),
        gps_accuracy_m, ward_id, media_keys[], raw_text, transcript, lang,
        category, subcategory, severity_raw, hazard_flags[], summary,
        embedding VECTOR(768), status, incident_id FK NULL)

incidents(incident_id PK, title, category, centroid GEOGRAPHY(POINT), ward_id,
          first_reported_at, last_reported_at, report_count, distinct_reporters,
          priority_score, priority_band, factor_breakdown JSONB, why TEXT,
          department_id FK, cc_departments[], sla_due_at, status, super_incident_id FK NULL)

super_incidents(super_incident_id PK, hypothesis TEXT, confidence, ward_id,
                window_start, window_end, member_incident_ids[], status)

evidence(evidence_id PK, incident_id FK, kind, source_uri, verdict, confidence, created_at)

resolutions(resolution_id PK, incident_id FK, claimed_by, claimed_at,
            citizen_confirmations, citizen_disputes, visual_verdict, final_status, verified_at)

verification_results(verdict_id PK, message_id, trace_id, topic, agent, layer,
                     verdict, reasons JSONB, judge_model, created_at)

agent_runs(run_id PK, agent, version, message_id, trace_id, started_at, ended_at,
           tokens_in, tokens_out, cost_estimate, error)
```

`agent_runs` + `verification_results` + `trace_id` together give a full audit trail: for any incident, show every agent decision, its inputs, its confidence, and its Sentinel verdict.

---

## 11. Repository structure

```text
CivicAI-IBM/
├── PRD.md
├── README.md
├── docker-compose.yml
├── .env.example
├── docs/
│   ├── architecture.md
│   ├── message-contracts.md
│   ├── sentinel.md
│   └── demo-script.md
├── schemas/                  # JSON Schema per topic + version (source of truth)
│   ├── envelope.v1.json
│   ├── reports.ingested.v1.json
│   └── ...
├── bus/                      # transport abstraction (redis impl + in-memory test impl)
├── common/                   # envelope helpers, config, tracing, llm provider, db
├── agents/
│   ├── a0_intake/
│   ├── a1_perception/
│   ├── a2_dedup/
│   ├── a3_synthesis/
│   ├── a4_priority/
│   ├── a5_routing/
│   ├── a6_resolution/
│   ├── a7_evidence/
│   └── av_sentinel/
│       ├── layers/{structural,invariants,judge,continuous}.py
│       ├── rules/            # per-agent invariant definitions
│       └── goldens/
├── api/                      # FastAPI: citizen + dashboard + trace endpoints
├── web/
│   ├── citizen/              # PWA
│   └── dashboard/            # department + admin + trace viewer
├── data/
│   ├── seed/                 # synthetic Bengaluru reports incl. drainage scenario
│   ├── goldens/              # labelled eval sets
│   └── sample_cctv/
└── tests/
```

---

## 12. API surface (v1)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/reports` | Citizen submission (multipart) → `{report_id, trace_id, matched_incident?}` |
| `GET` | `/v1/reports/{id}` | Status + linked incident |
| `GET` | `/v1/incidents` | Filter by ward, department, band, status; sorted by priority |
| `GET` | `/v1/incidents/{id}` | Full record + evidence bundle + factor breakdown |
| `POST` | `/v1/incidents/{id}/resolve` | Department claims resolution |
| `POST` | `/v1/incidents/{id}/confirm` | Citizen confirms or disputes, optional after-photo |
| `GET` | `/v1/super-incidents` | Root-cause hypotheses |
| `GET` | `/v1/trace/{trace_id}` | Full agent decision DAG + Sentinel verdicts |
| `GET` | `/v1/health/agents` | Per-agent liveness, lag, verdict mix |

---

## 13. Milestones

| Phase | Deliverable | Done when |
|-------|-------------|-----------|
| **M0 — Skeleton** | Repo, compose, Postgres+PostGIS+pgvector, bus abstraction, envelope + schemas, one echo agent | An event flows A0 → echo → Sentinel L1 and is visible in the trace viewer |
| **M1 — Core path** | A0, A1, A2, A3 consolidation | 20 seeded reports collapse into the expected incidents |
| **M2 — Decisioning** | A4, A5 | Ranked department dashboards with explainable scores |
| **M3 — Sentinel** | AV L1+L2, goldens, quarantine | A deliberately broken agent build is blocked by CI |
| **M4 — Pattern + closure** | A3 pattern mode, A6 | Drainage SuperIncident appears; one incident closed and verified |
| **M5 — Judge + evidence** | AV L3/L4, A7 sample-footage mode | Judge verdicts on every event in the demo run |
| **M6 — Demo polish** | PWA flow, map, trace viewer, seeded story | 5-minute demo runs clean twice in a row |

---

## 14. Success metrics

- **Compression ratio:** reports ÷ incidents (target ≥ 3× on seeded data).
- **Dedup recall / false-merge rate:** ≥ 0.80 / ≤ 0.05 on goldens.
- **Routing accuracy:** ≥ 0.90.
- **Priority agreement:** Spearman ρ ≥ 0.7 vs. human ranking of 30 incidents.
- **Sentinel coverage:** 100% of events carry a verdict in strict mode.
- **Escape rate:** invariant violations reaching the dashboard = 0.
- **Time to first meaningful triage:** report → routed incident < 60 s p95.

## 15. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| False merges hide distinct problems | High | Conservative auto-merge threshold, LLM adjudication in the grey zone, A2 invariant checks, one-click un-merge |
| Brigading inflates priority | Medium | Device-deduplicated, log-scaled corroboration; hazard floor independent of counts |
| LLM cost/latency at volume | Medium | Cheap deterministic gates first; LLM only in the grey zone; batch embeddings; cache by content hash |
| CCTV access unavailable | Low (by design) | A7 is optional; demo uses sample footage |
| Judge agrees with a wrong agent | Medium | Different model/config for judge, plus deterministic L2 invariants that don't depend on any model |
| PII in citizen media | High | Strip EXIF except geotag, blur faces/plates on ingest, retention policy on raw media |
| Hackathon scope creep | High | M0–M4 is the demo; M5–M6 is polish. Nothing outside §4 goals gets built. |

## 16. Open questions

1. Real BBMP complaint feed or fully synthetic seed data for the demo?
2. Which IBM watsonx models are available on the track, and what are the rate limits?
3. Kannada ASR quality — do we need a fallback to text entry?
4. Does the demo need multi-ward scale, or is one ward with a rich story stronger?
5. Do we show the Sentinel trace viewer to judges as a first-class feature? (Recommended: yes — verifiable AI is a differentiator, not plumbing.)

---

## 17. Demo narrative (5 minutes)

1. Four citizens report the same pothole in different words, one in Kannada, two with photos → dashboard shows **one** incident, 4 reports, priority 93 with its reason.
2. Officer opens the evidence bundle; trace viewer shows every agent decision and its Sentinel verdict.
3. Seeded drainage scenario fires → **SuperIncident: possible underlying drainage failure**, linking five different complaint categories.
4. Officer marks resolved → citizen uploads an after-photo → before/after comparison → **resolution verified**.
5. Inject a deliberately corrupted agent output live → Sentinel quarantines it, dashboard stays clean.
