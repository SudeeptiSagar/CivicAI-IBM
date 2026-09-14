-- Sentinel hardening (PRD section 8, milestone M3).
--
-- verification_results and quarantine already exist (0001_init.sql) and are
-- exactly what PRD section 8.2 asks Sentinel to write to. One table is
-- genuinely new here:
--
--   sentinel_fail_soft_retries  bounds the fail_soft retry Sentinel triggers
--                                (agents/av_sentinel/agent.py) to exactly one
--                                per message. Without a durable record of "this
--                                one has already been retried", a redelivered
--                                fail_soft verdict (at-least-once verification,
--                                same as everything else on the bus) would
--                                retry the producer forever instead of
--                                escalating to quarantine on the second miss.

BEGIN;

CREATE TABLE sentinel_fail_soft_retries (
    message_id  UUID PRIMARY KEY,
    trace_id    UUID        NOT NULL,
    topic       TEXT        NOT NULL,
    agent       TEXT        NOT NULL,
    reason      TEXT        NOT NULL,
    retried_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE sentinel_fail_soft_retries IS
    'One row per message Sentinel has already retried once via fail_soft. A second fail_soft on the same message escalates to quarantine instead of retrying again.';

CREATE INDEX sentinel_fail_soft_retries_trace_idx ON sentinel_fail_soft_retries (trace_id);

COMMIT;
