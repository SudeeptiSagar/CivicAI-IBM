# Message contracts

Companion to PRD sections 9.2, 9.3 and 11. The PRD defines the envelope and the
topic table; this document records the decisions made while turning that into
`schemas/`, **specifically the places where the PRD was silent and an
implementation choice was required**.

Everything below is a decision, not a requirement. If a decision conflicts with
what the team wants, change it here and in `schemas/` together.

---

## Where the schemas live

`schemas/` is the source of truth (PRD section 11). `common/envelope.py` is a
convenience model over the same contract, and `tests/test_envelope.py` fails if
the two drift — the model's field set must equal the envelope schema's, and a
model-built envelope must pass Sentinel L1.

Schema files are named `<topic>.v<major>.json`. `schema_version` resolves by
**major version only**: `1.0.0` and `1.7.3` both load `v1`, because payloads are
additive-only within a major version (PRD section 9.2).

## Patterned topic families

PRD section 9.3 lists `quarantine.<topic>` and `control.<agent>` as patterns
rather than fixed names, and PRD section 7 mentions `*.skipped` events without
naming them. Rather than enumerate every member, three family schemas cover them:

| Family | Matches | Schema |
|---|---|---|
| skipped | any topic ending `.skipped` | `schemas/skipped.v1.json` |
| quarantine | any topic starting `quarantine.` | `schemas/quarantine.v1.json` |
| control | any topic starting `control.` | `schemas/control.v1.json` |

A bare prefix (`quarantine.`, `.skipped`) does **not** resolve — otherwise a
malformed topic name would be handed a valid schema.

Unlike concrete topics, family schemas do not pin a `topic` const, since the
member name varies.

**Decision:** `*.skipped` is a suffix on the topic the agent would otherwise
have produced — an A1 skip publishes to `reports.understood.skipped`. This
keeps the skip adjacent to the output it replaced.

## Decisions made where the PRD is silent

### Enumerations

The PRD names categories, departments and hazard flags in prose but never
enumerates them. These are now closed enums in `schemas/envelope.v1.json`
`$defs`, so an off-taxonomy value fails L1 rather than reaching a dashboard.

| Def | Values | Source |
|---|---|---|
| `category` | 14 values (`pothole`, `waterlogging`, `drain_overflow`, `sewage`, `garbage`, `streetlight`, …, `other`) | Extrapolated from the complaint types named across PRD sections 2, 7 and 17 |
| `department` | 6 values, exactly the list in PRD section 7/A5 | PRD |
| `hazard_flag` | 10 values. The life-safety subset is `open_manhole`, `live_wire`, `collapsed_structure`, `gas_leak` | First three named in PRD section 7/A4; the rest extrapolated |

`priority_band` is `critical` / `high` / `medium` / `low`. The PRD refers to
"priority band" and to SLA assignment "by (department, priority band)" but never
lists the bands.

Incident `status` is `open`, `routed`, `in_progress`, `resolution_claimed`,
`verified`, `reopened`, `needs_human_review`. Derived from the lifecycle in PRD
sections 7/A3, 7/A6 and the `needs_human_review` flag in section 8.2.

### Payload shapes

Payloads were derived from the agent descriptions in PRD section 7 and the
table definitions in PRD section 10. Two shapes needed a judgement call:

**SuperIncidents ride on `incidents.updated`.** PRD section 7/A3 describes
pattern detection producing a `SuperIncident`, but section 9.3 gives A3 only one
output topic. Rather than invent a topic, `incidents.updated` carries a `mode`
discriminator (`consolidation` | `pattern`) and an optional `super_incident`
object. This preserves the PRD's rule that a hypothesis is advisory and never
removes a member incident.

**`reports.rejected` allows a null `report_id`.** A0 may reject a submission
before minting an id (malformed request), so the field is required-but-nullable
rather than absent.

### Envelope details

- `causation_id` is **required but nullable**. Null means trace-originating
  (A0 intake, or a dashboard action); every other message must name its cause.
- `additionalProperties: false` everywhere, envelope and payload alike. A
  renamed or misspelled field fails loudly at L1 instead of silently arriving as
  `None` in a downstream agent.
- `date-time` is genuinely enforced. jsonschema treats unknown formats as
  annotations by default, so `common/schemas.py` registers a real checker —
  without it a malformed `emitted_at` would pass L1.
- `reports.ingested` carries a `source` field (`pwa` | `api` | `canary`). The
  `canary` value exists so Sentinel L4's injected synthetic reports (PRD section
  8.1) are distinguishable from citizen traffic.

## What L1 does and does not check

L1 is structural and deterministic: schema conformance, enum membership, ranges,
`confidence ∈ [0,1]`, non-null ids, and that `(topic, schema_version)` resolves
to a registered contract at all. It has only two verdicts — `pass` and
`fail_hard` — because a message that does not match its declared contract cannot
be reasoned about.

L1 does **not** check business invariants. Everything in the PRD section 8.1 L2
table — report counts matching actual members, breakdown weights summing to
1.0, the life-safety floor being honoured, category agreement across an
incident's members — is L2, and arrives in P4.

Sentinel never mutates what it inspects (PRD section 8.2). `verify_structural()`
returns a verdict; acting on it is the caller's job.
