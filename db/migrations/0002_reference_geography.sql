-- Reference geography and intake bookkeeping for A0 (PRD section 7/A0).
--
-- A0 must resolve a report's location to a ward via a PostGIS polygon join,
-- and must reject anything outside the city boundary (PRD section 8.1, A0
-- invariants). Both need polygons, which PRD section 10 does not model because
-- they are reference data rather than pipeline state.
--
-- The polygons themselves are loaded by scripts/load_reference_data.py from
-- data/reference/bengaluru.geojson. They are SIMPLIFIED APPROXIMATIONS, not
-- official BBMP boundaries -- see that file's header.

BEGIN;

CREATE TABLE city_boundary (
    city_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    geom       GEOGRAPHY(POLYGON, 4326) NOT NULL,
    source     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE city_boundary IS
    'City extent. A0 rejects reports outside it (PRD 8.1 A0 invariant).';
COMMENT ON COLUMN city_boundary.source IS
    'Provenance of the geometry. Simplified fixtures must say so here.';

CREATE INDEX city_boundary_geom_idx ON city_boundary USING GIST (geom);


CREATE TABLE wards (
    ward_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    zone       TEXT NULL,
    city_id    TEXT NOT NULL REFERENCES city_boundary (city_id),
    geom       GEOGRAPHY(POLYGON, 4326) NOT NULL,
    source     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE wards IS
    'Ward polygons for the A0 ward/zone lookup and A5 jurisdiction routing.';

CREATE INDEX wards_geom_idx ON wards USING GIST (geom);
CREATE INDEX wards_city_idx ON wards (city_id);

-- reports.ward_id and incidents.ward_id become real references now that the
-- table they point at exists.
ALTER TABLE reports   ADD CONSTRAINT reports_ward_fk
    FOREIGN KEY (ward_id) REFERENCES wards (ward_id);
ALTER TABLE incidents ADD CONSTRAINT incidents_ward_fk
    FOREIGN KEY (ward_id) REFERENCES wards (ward_id);


-- ---------------------------------------------------------------------------
-- Intake attempts
-- ---------------------------------------------------------------------------

-- PRD 7/A0 rate-limits per device "to blunt spam/brigading". Counting rows in
-- `reports` would only count submissions that passed validation, so a flood of
-- malformed ones would sail past the limit. Every attempt lands here.
CREATE TABLE intake_attempts (
    attempt_id  UUID PRIMARY KEY,
    device_hash TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome     TEXT        NOT NULL
                CHECK (outcome IN ('accepted', 'rejected', 'rate_limited')),
    reason_code TEXT        NULL
);

CREATE INDEX intake_attempts_device_idx ON intake_attempts (device_hash, created_at DESC);

COMMIT;
