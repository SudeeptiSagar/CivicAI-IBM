# Sentinel

Companion to PRD section 8. What is implemented today, what is not, and the
decisions taken where the PRD is silent.

---

## What runs today

**L1 — structural.** Every message on every topic is validated against the
JSON Schema registered for its `(topic, schema_version)`. Required fields,
enums, ranges, `confidence ∈ [0,1]`, non-null ids, and — importantly — whether
the topic resolves to a registered contract at all.

L1 has two verdicts, `pass` and `fail_hard`. There is no soft verdict, because
a message that does not match its declared contract cannot be reasoned about.
On `fail_hard` the envelope is copied to the `quarantine` table and
republished to `quarantine.<topic>` for human triage.

**L2 — invariant / business rules (P4).** Runs after L1 passes, for the seven
topics PRD 8.1's table names invariants for: `reports.ingested` (A0),
`reports.understood` (A1), `reports.linked` (A2), `incidents.updated` (A3),
`incidents.prioritized` (A4), and `incidents.routed` / `incidents.unrouted`
(A5). `agents/av_sentinel/layers/invariants.py` dispatches to one rule module
per agent in `agents/av_sentinel/rules/` — `layers.invariants.has_rules_for(topic)`
says which topics get an L2 verdict at all; a topic with no rules gets only
its L1 verdict, not a rubber-stamp second `pass`.

Every rule module documents, per invariant, whether it is checked from the
envelope alone or needs a database (an incident's other members, whether an
id has been seen before, a previous score). DB-gated checks run only when
Sentinel has `persist=True`; without a database they are silently skipped,
never guessed at — consistent with this repo's "never invent what you could
not read" rule.

**A6 and A7 are not covered.** Those agents do not exist yet (P5, P6) — there
is nothing to verify an invariant against. `layers/invariants.py`,
`rules/__init__.py`, and this file all say so explicitly rather than silently
omitting two rows of PRD 8.1's table.

**L3 (LLM judge) and L4 (continuous checks, beyond the golden-set gate below)
are not implemented.** They are P6. `GET /v1/health/agents` reports
`sentinel_layers_active: ["L1", "L2"]` so nothing in the system overstates its
own coverage.

## The verdict gate: Sentinel now sits in front of consumers, not beside them

This closes the caveat that used to be here. Before P4, Sentinel and each
consuming agent held independent consumer groups on the same topic, so an
agent could begin work on a message Sentinel was about to quarantine.

`common/verdict_gate.py` implements PRD section 8.3's fallback #1: a consumer
waits for Sentinel's verdict on a specific message before acting on it. It
polls `verification_results` directly by `message_id` — not a second bus
subscription — so the same gate logic runs identically against `bus/memory.py`
and `bus/redis.py` with no changes to either.

* `strict` — wait up to `Settings.sentinel_deadline_ms` (default 2000) for
  `pass`/`warn`. `fail_soft`/`fail_hard` within the deadline: the delivery is
  dropped (acked, `handle()` never runs) — Sentinel already reacted to it. No
  verdict by the deadline: the delivery is deferred (nacked), not treated as a
  handler failure, so it still climbs the same poison-pill budget on
  redelivery rather than waiting forever for a verdict that never arrives.
* `permissive` — same, but proceeds if the deadline lapses with no verdict.
  The gap is logged.
* `shadow` — never waits, never blocks. Sentinel still verifies and records
  independently.

**Off by default** (`Settings.sentinel_gate_enabled = False`), and only
constructed at all when an agent has `persist=True` — the gate needs
`verification_results`, which needs a database. Every existing test and
deployment keeps its pre-P4 behaviour unless this flag is turned on
deliberately. `agents/base.py`'s class docstring has the full wiring; the
gate's own decision logic is unit-tested in isolation in
`tests/test_verdict_gate.py` (mode switching, the deadline, no real sleeping),
and its integration into `Agent._process_envelope` in
`tests/test_verdict_gate_wiring.py`.

**Honest limit:** with the flag on, `strict` mode's 2-second deadline is
enforced by polling Postgres at `Settings.sentinel_gate_poll_ms` (default
50ms) intervals, not by subscribing to Redis Streams' consumer-group
semantics for `verification.results` directly. That was a deliberate choice
(see `common/verdict_gate.py`'s module docstring) — a per-agent, per-message
bus subscription matched back to the delivery it is waiting on is a larger
refactor than P4 budgets for, and would duplicate the durable store Sentinel
already writes to. The trade-off is real: under Redis Streams' at-least-once
delivery and unbounded consumer lag, a verdict that is slow to be *recorded*
(not just slow to be *computed*) delays every gated consumer by however long
that write takes, bounded by the deadline. This has not been load-tested.

## `fail_soft`: Sentinel retries the producer, once

Distinct from `agents.base.Agent`'s own three-attempt retry budget, which is
for *transport or handler* failures — an exception `handle()` raised.
`fail_soft` is a *verification* failure: the handler ran fine and produced a
plausible output that an L2 rule says is wrong in a retryable way (a summary
over the 20-word cap, for instance — see `agents/av_sentinel/rules/a1.py`'s
classification of which invariants are `fail_hard` vs `fail_soft` vs `warn`).

On `fail_soft`, `Sentinel._fail_soft_retry` clears the producing agent's
`handler_results` idempotency row for the message that *caused* the failed
output, and republishes that cause message to the producer's own input topic
with the failure reason appended to its `rationale`. One retry only, tracked
in `sentinel_fail_soft_retries` (migration `0004`): a second `fail_soft` on
the same message escalates straight to quarantine instead of retrying
forever. Needs `persist=True`; without a database there is no idempotency
record to clear and no archived cause message to replay, so a `fail_soft`
verdict is still recorded and published, but nothing is retried — logged, not
silently dropped.

## Quarantine triage

`GET /v1/quarantine` (filter by `status`, default `pending`, and `topic`),
`GET /v1/quarantine/{id}` (full record, envelope included),
`POST /v1/quarantine/{id}/release` (re-publish the envelope to its original
topic — it goes through Sentinel again on the way through, so a release does
not bypass verification, it gives the envelope another chance to pass), and
`POST /v1/quarantine/{id}/discard` (close without re-publishing). Both actions
require a `reviewed_by` and are terminal — a `quarantine` row already
`released` or `discarded` cannot be reviewed again (409). See `api/main.py`
and `api/queries.py`.

## The CI regression gate

`scripts/evaluate.py` (ROADMAP Lane 3) is two checks, wired into
`.github/workflows/ci.yml` as its own step:

1. A **meta-check**: every known-bad fixture from `tests/test_structural.py`
   must still fail L1. This is the direct implementation of PRD milestone
   M3's acceptance criterion ("a deliberately broken agent build is blocked
   by CI") — if Sentinel ever passes a payload it must reject, this exits
   non-zero immediately. It is also the seed of the fuller PRD 8.2 meta-check
   (self-alert, degrade to `strict`), which is P6.
2. A **golden-set replay** against `agents/av_sentinel/goldens/reports.json`
   (~50 hand-labelled reports, 15 known duplicate clusters, including
   Kannada/Hindi fixtures that exercise the "no ASR/vision provider, degrade
   honestly" path rather than real multilingual understanding — see that
   file's `_language_note`), compared against the recorded baseline in
   `agents/av_sentinel/goldens/baseline.json`. Blocks on regression, not on
   an absolute PRD 14 target — see `scripts/evaluate.py`'s module docstring
   for why, and for the measured (not merely described) gap in the lexical
   embedding baseline's dedup recall on this set.

This is an offline replay of `common.matching`'s scoring formula against the
golden fixtures, not a run through the deployed agents and infrastructure —
`scripts/evaluate.py`'s docstring is explicit about the difference.

## Decisions where the PRD is silent

### Sentinel does not verify its own output

`verification.results` and `sentinel.alert` are excluded from the topics
Sentinel watches, and `quarantine.*` is excluded by not being a concrete topic.
Verifying its own verdicts would emit a verdict per verdict, without end.

PRD section 8.2 already covers Sentinel differently — with a meta-check that
replays known-bad fixtures and degrades Sentinel to `strict` if it passes
something it should have failed. `scripts/evaluate.py`'s meta-check step is
the seed of this; the fuller version (self-alert, automatic degrade) arrives
with L4 in P6.

### The skip family is watched

PRD section 9.3 enumerates concrete topics, but PRD section 7 lets any agent
emit `<topic>.skipped`. A skip is a real decision, so Sentinel derives and
watches the skip topic for every concrete topic it watches. Skip topics have
no L2 rules (`has_rules_for` returns False for them), since PRD 8.1's
invariant table is about an agent's business output, not its decision to
produce none.

### Failure reasons are truncated

`jsonschema` quotes the offending value in full. A 768-float embedding of the
wrong length produced a multi-kilobyte reason string, which overflowed the
envelope's own 2000-character `rationale` limit — so Sentinel crashed on
exactly the malformed input it exists to catch. Reasons are now capped at 400
characters at the source (`MAX_REASON_LENGTH`). The head of a jsonschema
message carries the diagnosis; the tail is the offending value.

### Sentinel writes to `messages`, and now to `sentinel_fail_soft_retries`

PRD section 8.2 says Sentinel writes only to `verification_results`,
`quarantine` and `sentinel_alerts`. It also archives its own emitted envelopes
to `messages`, like every other publisher, and (P4) writes one row to
`sentinel_fail_soft_retries` per message it has retried via `fail_soft`.

Both are judged consistent with the rule's intent: `messages` is the audit
trail, not business data, and `sentinel_fail_soft_retries` exists purely to
bound Sentinel's own retry behaviour to one attempt — it holds no business
data either, and the constraint exists to stop Sentinel *fixing* business
records, not to stop it tracking its own actions. No code path in
`agents/av_sentinel/` touches `reports`, `incidents`, `resolutions` or any
other business table.
