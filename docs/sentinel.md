# Sentinel

Companion to PRD section 8. What is implemented today, what is not, and the
decisions taken where the PRD is silent.

---

## What runs today

**L1 — structural only.** Every message on every topic is validated against the
JSON Schema registered for its `(topic, schema_version)`. Required fields,
enums, ranges, `confidence ∈ [0,1]`, non-null ids, and — importantly — whether
the topic resolves to a registered contract at all.

L1 has two verdicts, `pass` and `fail_hard`. There is no soft verdict, because
a message that does not match its declared contract cannot be reasoned about.
On `fail_hard` the envelope is copied to the `quarantine` table and republished
to `quarantine.<topic>` for human triage.

L2 invariants, the L3 LLM judge and L4 continuous checks are **not
implemented**. They are P4 and P6. `GET /v1/health/agents` reports
`sentinel_layers_active: ["L1"]` so nothing in the system overstates its own
coverage.

## Known limitation: Sentinel does not yet gate consumers

This is the gap most worth understanding before relying on P1.

PRD section 8 says Sentinel "sits between producers and consumers logically:
downstream agents only act on events carrying a valid Sentinel verdict, or on
events whose verification deadline has lapsed in `permissive` mode."

**In P1 it sits beside them, not in front of them.** Sentinel and the consuming
agents each have their own consumer group on the same topic, so both receive a
message at the same time. An agent can begin work on a message that Sentinel is
about to quarantine.

In practice a malformed envelope is usually rejected by the consuming agent too
— `common.envelope.Envelope` forbids unknown fields and range-checks
`confidence`, so most `fail_hard` envelopes fail to parse and are deadlettered
by the agent independently. But that is a second line of defence that happens
to overlap, not the gate the PRD describes: a payload-level violation (a
768-dimension embedding arriving with 512 values, a category outside the
taxonomy) parses fine as an envelope and would reach a handler.

So the effective behaviour today is closest to `permissive` with a zero
deadline, regardless of `CIVICAI_SENTINEL_MODE`. `GET /v1/health/agents`
reports this as `sentinel_gate_enforced: false` rather than letting the
configured mode imply a guarantee.

Closing it needs one of:

1. consumers waiting for a verdict on `verification.results` before acting,
   with the `permissive` deadline from PRD section 8.3; or
2. Sentinel verifying and forwarding onto a separate verified topic, which
   changes every agent's input topic.

This is scheduled with the rest of the Sentinel work in **P4 (M3)**, whose
acceptance criterion — "a deliberately broken agent build is blocked by CI" —
requires the gate to be real.

## Decisions where the PRD is silent

### Sentinel does not verify its own output

`verification.results` and `sentinel.alert` are excluded from the topics
Sentinel watches, and `quarantine.*` is excluded by not being a concrete topic.
Verifying its own verdicts would emit a verdict per verdict, without end.

PRD section 8.2 already covers Sentinel differently — with a meta-check that
replays known-bad fixtures and degrades Sentinel to `strict` if it passes
something it should have failed. That arrives with L4 in P6. The seed of it
already exists as the bad-payload table in `tests/test_structural.py`.

### The skip family is watched

PRD section 9.3 enumerates concrete topics, but PRD section 7 lets any agent
emit `<topic>.skipped`. A skip is a real decision, so Sentinel derives and
watches the skip topic for every concrete topic it watches.

This was a live bug, not a hypothetical: with only the concrete topics
enumerated, the echo agent's skip event went through the whole pipeline with no
verdict. It was caught by running the stack, not by the test suite, because the
integration test passed Sentinel an explicit topic list. Both a unit test and
an end-to-end test now cover it.

### Failure reasons are truncated

`jsonschema` quotes the offending value in full. A 768-float embedding of the
wrong length produced a multi-kilobyte reason string, which overflowed the
envelope's own 2000-character `rationale` limit — so Sentinel crashed on
exactly the malformed input it exists to catch. Reasons are now capped at 400
characters at the source (`MAX_REASON_LENGTH`). The head of a jsonschema
message carries the diagnosis; the tail is the offending value.

### Sentinel writes to `messages`

PRD section 8.2 says Sentinel writes only to `verification_results`,
`quarantine` and `sentinel_alerts`. It also archives its own emitted envelopes
to `messages`, like every other publisher.

This is judged consistent with the rule's intent: `messages` is the audit trail,
not business data, and the constraint exists to stop Sentinel *fixing* things.
No code path in `agents/av_sentinel/` touches `reports`, `incidents`,
`resolutions` or any other business table — a property worth keeping true, and
worth a test once those tables have writers in P2.
