-- Reference data for A4 Prioritization and A5 Routing (PRD section 7/A4, 7/A5).
--
-- A4's exposure and vulnerable-site-proximity factors need something to measure
-- distance against, and none of PRD section 10's tables carry it. Two small
-- reference tables are added here, in the same spirit as 0002's city/ward
-- geometry: honestly-labelled reference data, not pipeline state.
--
--   poi              schools and hospitals. A4's vulnerable_site_proximity
--                    factor is a genuine PostGIS distance computation against
--                    these points -- never call it a model.
--   ward_road_class  a coarse per-ward proxy for exposure (how much footfall/
--                    traffic a location sees). A real road network join
--                    (individual road segments with their own class) would be
--                    the correct long-term shape, but building and seeding one
--                    is out of scope for this phase; a ward-level lookup is the
--                    simplest honest stand-in and is labelled as a proxy in
--                    A4's code, not disguised as precise geometry.
--
-- The POI points loaded from data/reference/bengaluru_poi.geojson are a SMALL,
-- DETERMINISTIC, SYNTHETIC set placed inside the existing fixture wards'
-- bounding boxes. They are NOT real BBMP or OSM data -- see that file's header.

BEGIN;

CREATE TABLE poi (
    poi_id     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('school', 'hospital')),
    name       TEXT NOT NULL,
    geom       GEOGRAPHY(POINT, 4326) NOT NULL,
    source     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE poi IS
    'Vulnerable-site points (schools, hospitals) for A4''s proximity factor. '
    'Synthetic fixture data unless source says otherwise -- see data/reference/bengaluru_poi.geojson.';
COMMENT ON COLUMN poi.source IS
    'Provenance of the point. Synthetic fixtures must say so here, never "official".';

CREATE INDEX poi_geom_idx ON poi USING GIST (geom);
CREATE INDEX poi_kind_idx ON poi (kind);


CREATE TABLE ward_road_class (
    ward_id            TEXT PRIMARY KEY REFERENCES wards (ward_id),
    dominant_road_class TEXT NOT NULL
                        CHECK (dominant_road_class IN ('arterial', 'collector', 'local')),
    source             TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ward_road_class IS
    'Coarse ward-level proxy for A4''s exposure factor: NOT a real road network '
    'join. A ward with no row here is treated as unknown road class by A4.';

COMMIT;
