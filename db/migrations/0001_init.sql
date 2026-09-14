-- CivicAI initial schema.
--
-- The seven core tables come from PRD section 10. Four more are added here;
-- each is justified in docs/data-model.md rather than slipped in silently:
--
--   departments      PRD 7/A5 makes "department in registry" an L2 invariant,
--                    and incidents.department_id is declared FK - both imply a
--                    registry table
--   messages         the envelope archive. PRD 12 requires /v1/trace/{id} to
--                    return the "full agent decision DAG", which needs the
--                    causation_id edges; Redis streams are trimmable, and PRD
--                    6.1 says all durable truth lives in Postgres
--   quarantine       PRD 8.2 names it as one of Sentinel's three write targets
--   sentinel_alerts  likewise
--
-- Enumerated columns use TEXT + CHECK rather than native PG enums: the value
-- lists are owned by schemas/ and native enums are painful to alter. A test
-- compares every list below against the JSON Schema it mirrors.

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ---------------------------------------------------------------------------
-- Registries
-- ---------------------------------------------------------------------------

CREATE TABLE departments (
    department_id   TEXT PRIMARY KEY,
    display_name    TEXT        NOT NULL,
    default_sla_hours DOUBLE PRECISION NOT NULL CHECK (default_sla_hours > 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE departments IS
    'Department registry from PRD 7/A5. Routing may only emit a department_id present here.';

-- The six departments named in PRD 7/A5. SLA defaults are an implementation
-- decision (the PRD sets SLA "by (department, priority band)" without values)
-- and are overridden per priority band by A5 in P3.
INSERT INTO departments (department_id, display_name, default_sla_hours) VALUES
    ('roads_and_infrastructure', 'Roads & Infrastructure', 72),
    ('drainage_and_water',       'Drainage / Water',       48),
    ('solid_waste_management',   'Solid Waste Management', 24),
    ('electrical_streetlights',  'Electrical / Streetlights', 48),
    ('health',                   'Health',                 24),
    ('parks',                    'Parks',                  120);


-- ---------------------------------------------------------------------------
-- Message archive (audit trail and trace DAG)
-- ---------------------------------------------------------------------------

CREATE TABLE messages (
    message_id      UUID PRIMARY KEY,
    trace_id        UUID        NOT NULL,
    causation_id    UUID        NULL,
    correlation_id  TEXT        NOT NULL,
    topic           TEXT        NOT NULL,
    schema_version  TEXT        NOT NULL,
    emitted_at      TIMESTAMPTZ NOT NULL,
    producer_agent  TEXT        NOT NULL,
    producer_version TEXT       NOT NULL,
    producer_model  TEXT        NULL,
    confidence      DOUBLE PRECISION NOT NULL
                    CHECK (confidence >= 0 AND confidence <= 1),
    rationale       TEXT        NOT NULL,
    envelope        JSONB       NOT NULL,
    archived_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE messages IS
    'Every envelope published, archived by the emitting agent. causation_id builds the trace DAG.';

CREATE INDEX messages_trace_idx       ON messages (trace_id, emitted_at);
CREATE INDEX messages_causation_idx   ON messages (causation_id);
CREATE INDEX messages_correlation_idx ON messages (correlation_id);
CREATE INDEX messages_topic_idx       ON messages (topic, emitted_at DESC);


-- ---------------------------------------------------------------------------
-- Incidents and super-incidents
-- ---------------------------------------------------------------------------

CREATE TABLE super_incidents (
    super_incident_id  UUID PRIMARY KEY,
    hypothesis         TEXT        NOT NULL,
    confidence         DOUBLE PRECISION NOT NULL
                       CHECK (confidence >= 0 AND confidence <= 1),
    ward_id            TEXT        NULL,
    window_start       TIMESTAMPTZ NOT NULL,
    window_end         TIMESTAMPTZ NOT NULL,
    member_incident_ids UUID[]     NOT NULL DEFAULT '{}',
    source             TEXT        NOT NULL DEFAULT 'seed_rule'
                       CHECK (source IN ('seed_rule', 'llm')),
    status             TEXT        NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'confirmed', 'dismissed')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT super_incident_window_ordered CHECK (window_end >= window_start)
);

COMMENT ON TABLE super_incidents IS
    'Root-cause hypotheses from A3 pattern mode. Advisory only: never removes a member incident (PRD 7/A3).';

CREATE TABLE incidents (
    incident_id       UUID PRIMARY KEY,
    title             TEXT        NOT NULL,
    category          TEXT        NOT NULL,
    centroid          GEOGRAPHY(POINT, 4326) NOT NULL,
    ward_id           TEXT        NULL,
    first_reported_at TIMESTAMPTZ NOT NULL,
    last_reported_at  TIMESTAMPTZ NOT NULL,
    report_count      INTEGER     NOT NULL DEFAULT 0 CHECK (report_count >= 0),
    distinct_reporters INTEGER    NOT NULL DEFAULT 0 CHECK (distinct_reporters >= 0),
    priority_score    DOUBLE PRECISION NULL
                      CHECK (priority_score IS NULL
                             OR (priority_score >= 0 AND priority_score <= 100)),
    priority_band     TEXT        NULL
                      CHECK (priority_band IS NULL
                             OR priority_band IN ('critical', 'high', 'medium', 'low')),
    factor_breakdown  JSONB       NULL,
    why               TEXT        NULL,
    department_id     TEXT        NULL REFERENCES departments (department_id),
    cc_departments    TEXT[]      NOT NULL DEFAULT '{}',
    sla_due_at        TIMESTAMPTZ NULL,
    status            TEXT        NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'routed', 'in_progress',
                                        'resolution_claimed', 'verified',
                                        'reopened', 'needs_human_review')),
    super_incident_id UUID        NULL REFERENCES super_incidents (super_incident_id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- PRD 8.1/A4: a score with no breakdown is rejected. Enforced here too so
    -- the invariant survives a direct write.
    CONSTRAINT incident_score_has_breakdown
        CHECK (priority_score IS NULL OR factor_breakdown IS NOT NULL),
    CONSTRAINT incident_reported_window_ordered
        CHECK (last_reported_at >= first_reported_at)
);

CREATE INDEX incidents_centroid_idx ON incidents USING GIST (centroid);
CREATE INDEX incidents_ward_idx     ON incidents (ward_id);
CREATE INDEX incidents_status_idx   ON incidents (status);
CREATE INDEX incidents_dept_idx     ON incidents (department_id);
-- The department queue view: ranked by priority within a department.
CREATE INDEX incidents_priority_idx ON incidents (department_id, priority_score DESC NULLS LAST);
CREATE INDEX incidents_sla_idx      ON incidents (sla_due_at) WHERE sla_due_at IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Reports
-- ---------------------------------------------------------------------------

CREATE TABLE reports (
    report_id      UUID PRIMARY KEY,
    trace_id       UUID        NOT NULL,
    device_hash    TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    geom           GEOGRAPHY(POINT, 4326) NOT NULL,
    gps_accuracy_m DOUBLE PRECISION NULL CHECK (gps_accuracy_m IS NULL OR gps_accuracy_m >= 0),
    ward_id        TEXT        NULL,
    media_keys     TEXT[]      NOT NULL DEFAULT '{}',
    raw_text       TEXT        NULL,
    transcript     TEXT        NULL,
    lang           TEXT        NULL,
    category       TEXT        NULL,
    subcategory    TEXT        NULL,
    severity_raw   SMALLINT    NULL CHECK (severity_raw IS NULL
                                           OR severity_raw BETWEEN 1 AND 5),
    hazard_flags   TEXT[]      NOT NULL DEFAULT '{}',
    summary        TEXT        NULL,
    embedding      VECTOR(768) NULL,
    status         TEXT        NOT NULL DEFAULT 'ingested'
                   CHECK (status IN ('ingested', 'understood', 'linked', 'rejected')),
    incident_id    UUID        NULL REFERENCES incidents (incident_id),
    source         TEXT        NOT NULL DEFAULT 'pwa'
                   CHECK (source IN ('pwa', 'api', 'canary')),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN reports.source IS
    'Marks Sentinel L4 canary injections (PRD 8.1) so synthetic traffic stays distinguishable.';

CREATE INDEX reports_geom_idx     ON reports USING GIST (geom);
CREATE INDEX reports_incident_idx ON reports (incident_id);
CREATE INDEX reports_trace_idx    ON reports (trace_id);
CREATE INDEX reports_created_idx  ON reports (created_at DESC);
CREATE INDEX reports_category_idx ON reports (category, created_at DESC);
CREATE INDEX reports_device_idx   ON reports (device_hash, created_at DESC);

-- A2 dedup searches by cosine distance (PRD 7/A2 semantic scoring). HNSW gives
-- better recall than IVFFlat and needs no training pass on an empty table.
CREATE INDEX reports_embedding_idx ON reports
    USING hnsw (embedding vector_cosine_ops);


-- ---------------------------------------------------------------------------
-- Evidence and resolution
-- ---------------------------------------------------------------------------

CREATE TABLE evidence (
    evidence_id UUID PRIMARY KEY,
    incident_id UUID        NOT NULL REFERENCES incidents (incident_id),
    kind        TEXT        NOT NULL
                CHECK (kind IN ('cctv_sample', 'citizen_media', 'third_party_feed')),
    source_uri  TEXT        NULL,
    verdict     TEXT        NOT NULL
                CHECK (verdict IN ('corroborating', 'contradicting', 'unavailable')),
    confidence  DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    notes       TEXT        NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE evidence IS
    'A7 output. Evidence can raise confidence or flag a contradiction; it can never auto-close an incident (PRD 7/A7).';

CREATE INDEX evidence_incident_idx ON evidence (incident_id);

CREATE TABLE resolutions (
    resolution_id         UUID PRIMARY KEY,
    incident_id           UUID        NOT NULL REFERENCES incidents (incident_id),
    claimed_by            TEXT        NOT NULL,
    claimed_at            TIMESTAMPTZ NOT NULL,
    citizen_confirmations INTEGER     NOT NULL DEFAULT 0 CHECK (citizen_confirmations >= 0),
    citizen_disputes      INTEGER     NOT NULL DEFAULT 0 CHECK (citizen_disputes >= 0),
    visual_verdict        TEXT        NULL
                          CHECK (visual_verdict IS NULL
                                 OR visual_verdict IN ('fixed', 'unchanged', 'inconclusive')),
    final_status          TEXT        NOT NULL DEFAULT 'claimed'
                          CHECK (final_status IN ('claimed', 'verified', 'disputed')),
    verified_at           TIMESTAMPTZ NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- PRD 7/A6: no verified state without two citizen confirmations or a
    -- confident visual match.
    CONSTRAINT resolution_verified_has_grounds CHECK (
        final_status <> 'verified'
        OR citizen_confirmations >= 2
        OR visual_verdict = 'fixed'
    )
);

CREATE INDEX resolutions_incident_idx ON resolutions (incident_id);


-- ---------------------------------------------------------------------------
-- Sentinel write targets (PRD 8.2). Sentinel writes here and nowhere else.
-- ---------------------------------------------------------------------------

CREATE TABLE verification_results (
    verdict_id  UUID PRIMARY KEY,
    message_id  UUID        NOT NULL,
    trace_id    UUID        NOT NULL,
    topic       TEXT        NOT NULL,
    agent       TEXT        NOT NULL,
    layer       TEXT        NOT NULL CHECK (layer IN ('L1', 'L2', 'L3', 'L4')),
    verdict     TEXT        NOT NULL
                CHECK (verdict IN ('pass', 'warn', 'fail_soft', 'fail_hard')),
    reasons     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    judge_model TEXT        NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One verdict per (message, layer): re-verifying is idempotent, not additive.
    CONSTRAINT verification_one_verdict_per_layer UNIQUE (message_id, layer)
);

CREATE INDEX verification_trace_idx   ON verification_results (trace_id, created_at);
CREATE INDEX verification_message_idx ON verification_results (message_id);
CREATE INDEX verification_verdict_idx ON verification_results (verdict, created_at DESC);

CREATE TABLE quarantine (
    quarantine_id  UUID PRIMARY KEY,
    message_id     UUID        NOT NULL,
    trace_id       UUID        NOT NULL,
    topic          TEXT        NOT NULL,
    verdict_id     UUID        NULL REFERENCES verification_results (verdict_id),
    envelope       JSONB       NOT NULL,
    reasons        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    status         TEXT        NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'released', 'discarded')),
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at    TIMESTAMPTZ NULL,
    reviewed_by    TEXT        NULL
);

COMMENT ON TABLE quarantine IS
    'Envelopes blocked by a fail_hard verdict, awaiting human triage. Downstream never sees these (PRD 8.2).';

CREATE INDEX quarantine_status_idx ON quarantine (status, quarantined_at DESC);
CREATE INDEX quarantine_trace_idx  ON quarantine (trace_id);

CREATE TABLE sentinel_alerts (
    alert_id   UUID PRIMARY KEY,
    kind       TEXT        NOT NULL
               CHECK (kind IN ('drift', 'canary_failure', 'loop_detected',
                               'meta_check_failure', 'quarantine_spike')),
    severity   TEXT        NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    detail     TEXT        NOT NULL,
    metric     TEXT        NULL,
    observed   DOUBLE PRECISION NULL,
    expected   DOUBLE PRECISION NULL,
    sigma      DOUBLE PRECISION NULL,
    subject_id TEXT        NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX sentinel_alerts_idx ON sentinel_alerts (created_at DESC);


-- ---------------------------------------------------------------------------
-- Agent telemetry
-- ---------------------------------------------------------------------------

CREATE TABLE agent_runs (
    run_id         UUID PRIMARY KEY,
    agent          TEXT        NOT NULL,
    version        TEXT        NOT NULL,
    message_id     UUID        NOT NULL,
    trace_id       UUID        NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ NULL,
    tokens_in      INTEGER     NULL CHECK (tokens_in IS NULL OR tokens_in >= 0),
    tokens_out     INTEGER     NULL CHECK (tokens_out IS NULL OR tokens_out >= 0),
    cost_estimate  NUMERIC(12, 6) NULL,
    outcome        TEXT        NOT NULL DEFAULT 'running'
                   CHECK (outcome IN ('running', 'ok', 'skipped', 'error')),
    error          TEXT        NULL,
    delivery_count INTEGER     NOT NULL DEFAULT 1 CHECK (delivery_count >= 1)
);

COMMENT ON TABLE agent_runs IS
    'One row per handled delivery. With verification_results and trace_id this is the audit trail (PRD 10).';

CREATE INDEX agent_runs_trace_idx   ON agent_runs (trace_id, started_at);
CREATE INDEX agent_runs_message_idx ON agent_runs (message_id);
CREATE INDEX agent_runs_agent_idx   ON agent_runs (agent, started_at DESC);


-- ---------------------------------------------------------------------------
-- Handler idempotency (PRD 9.4)
-- ---------------------------------------------------------------------------

CREATE TABLE handler_results (
    handler_key TEXT        NOT NULL,
    agent       TEXT        NOT NULL,
    result      JSONB       NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent, handler_key)
);

COMMENT ON TABLE handler_results IS
    'Durable idempotency store: a redelivery keyed the same is a no-op returning the prior result.';

COMMIT;
