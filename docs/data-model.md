# Data model

Companion to PRD section 10. That section defines seven core tables; this
document records what was added beyond them and why, plus the decisions the PRD
left open.

`db/migrations/0001_init.sql` is the source of truth. Enum values are mirrored
between SQL `CHECK` constraints and the JSON Schemas under `schemas/`, and
`tests/test_migrations.py` fails the build if the two drift apart.

---

## Tables from PRD section 10

`reports`, `incidents`, `super_incidents`, `evidence`, `resolutions`,
`verification_results`, `agent_runs` — created as specified, with the column
names the PRD uses.

## Tables added beyond PRD section 10

PRD section 10 is headed "core tables", not "all tables". Four more were needed:

### `departments`

PRD section 7/A5 makes "department ∈ registry" an L2 invariant and declares
`incidents.department_id` a foreign key. Both imply a registry table. Seeded
with exactly the six departments named in section 7/A5, and
`test_migrations.py` asserts the seeded ids equal the `department` enum in
`schemas/envelope.v1.json`.

`default_sla_hours` is an implementation decision: PRD section 7/A5 assigns SLA
"by (department, priority band)" without giving values. The column holds a
per-department default; A5 overrides it per band in P3.

### `messages`

The envelope archive — every message published, stored whole.

PRD section 12 requires `GET /v1/trace/{trace_id}` to return the "full agent
decision DAG". A DAG needs edges, and the edges are `causation_id`, which lives
in the envelope rather than in `agent_runs`. Redis streams are trimmed (the bus
caps them at 100k entries), and PRD section 6.1 puts all durable truth in
Postgres — so the archive, not the stream, is what makes a trace replayable
months later.

Whoever publishes, archives, via `common.messagelog.archive()`, always before
the publish: a message a consumer can see must already be in the audit trail.

### `quarantine` and `sentinel_alerts`

PRD section 8.2 names `verification_results`, `quarantine` and
`sentinel_alerts` as the three things Sentinel may write. Only the first was in
section 10.

### `handler_results`

Durable idempotency (PRD section 9.4). At-least-once delivery means a handler
will see the same event twice; this table makes the repeat a no-op that returns
the prior result, and makes that hold across a process restart.

---

## Decisions the PRD leaves open

| Decision | Choice | Why |
|---|---|---|
| Enum storage | `TEXT` + `CHECK` | Native PG enums are painful to alter, and the value lists are owned by `schemas/`. A test compares the two. |
| `reports.status` | `ingested`, `understood`, `linked`, `rejected` | One per pipeline stage in PRD section 7. |
| `incidents.status` | Matches the `incidents.updated` schema enum exactly | Drift here would let an agent emit a status the database rejects. |
| Migration tooling | Numbered `.sql` + a runner | Alembic's autogenerate does not model PostGIS or pgvector well; a hand-written file is the clearest record of what shipped. |
| Vector index | HNSW, `vector_cosine_ops` | Better recall than IVFFlat and needs no training pass on an empty table. A2 scores semantic similarity by cosine distance (PRD section 7/A2). |
| Geometry type | `GEOGRAPHY(POINT, 4326)` | `ST_DWithin` on geography takes metres directly, which is what A2's category radii are expressed in. |

## Invariants enforced in SQL

Sentinel is the primary gate, but three invariants are cheap to enforce at the
database too, so they survive a direct write:

* `incident_score_has_breakdown` — a `priority_score` requires a
  `factor_breakdown`. PRD section 8.1/A4: a score with no breakdown is rejected.
* `resolution_verified_has_grounds` — `final_status = 'verified'` requires two
  citizen confirmations or a `fixed` visual verdict (PRD section 7/A6).
* `verification_one_verdict_per_layer` — one verdict per (message, layer), so
  re-verifying is idempotent rather than additive.

## What is not modelled yet

`reports.embedding`, `incidents.factor_breakdown` and the rest of the columns
that later agents populate exist and are indexed, but nothing writes them until
A1–A5 arrive in P2 and P3. They are nullable for that reason.
