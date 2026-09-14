"""Load city/ward polygons and POI reference data into Postgres.

    python -m scripts.load_reference_data

Idempotent: re-running replaces the geometry for ids that already exist, so it
is safe to run after editing the GeoJSON.

The bundled geometry is a simplified fixture, not official BBMP data. See the
`_disclaimer` field in data/reference/bengaluru.geojson. The POI points loaded
here are likewise a synthetic fixture, not real BBMP/OSM data -- see the
`_disclaimer` field in data/reference/bengaluru_poi.geojson. Both are loaded by
one script and one compose step (`load-reference`) because they are the same
kind of thing: reference geography A4/A5 and A0 need, not pipeline output.

`ward_road_class` (A4's exposure proxy) has no source file of its own: it is a
tiny, hand-written table keyed to the fixture wards, seeded directly below.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

from common.db import transaction
from common.logging import configure_logging, get_logger

__all__ = ["POI_FILE", "REFERENCE_FILE", "load", "load_poi"]

log = get_logger(__name__)

REFERENCE_FILE = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "reference" / "bengaluru.geojson"
)
POI_FILE = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "reference" / "bengaluru_poi.geojson"
)

#: Coarse per-ward road-class proxy for A4's exposure factor (PRD 7/A4). Not a
#: real road network join -- see the migration comment on `ward_road_class`.
#: Assigned by hand against the fixture wards: Koramangala 5th Block and
#: Indiranagar sit on named arterial roads in reality, the others on smaller
#: internal roads, so they are marked collector/local.
WARD_ROAD_CLASS: dict[str, str] = {
    "BLR-151": "arterial",
    "BLR-152": "collector",
    "BLR-153": "local",
    "BLR-154": "collector",
    "BLR-160": "arterial",
}


def _polygon_wkt(geometry: dict[str, Any]) -> str:
    """GeoJSON polygon -> WKT. Outer ring only; the fixtures have no holes."""
    if geometry["type"] != "Polygon":
        raise ValueError(f"expected a Polygon, got {geometry['type']!r}")

    ring = geometry["coordinates"][0]
    points = ", ".join(f"{lon} {lat}" for lon, lat in ring)
    return f"POLYGON(({points}))"


def load(path: pathlib.Path = REFERENCE_FILE) -> tuple[int, int]:
    """Load the FeatureCollection. Returns (cities, wards) written."""
    document = json.loads(path.read_text(encoding="utf-8"))
    features = document["features"]

    cities = [f for f in features if f["properties"]["kind"] == "city"]
    wards = [f for f in features if f["properties"]["kind"] == "ward"]

    with transaction() as conn, conn.cursor() as cur:
        # Cities first: wards reference them.
        for feature in cities:
            properties = feature["properties"]
            cur.execute(
                """
                INSERT INTO city_boundary (city_id, name, geom, source)
                VALUES (%s, %s, ST_GeogFromText(%s), %s)
                ON CONFLICT (city_id) DO UPDATE
                SET name = EXCLUDED.name,
                    geom = EXCLUDED.geom,
                    source = EXCLUDED.source
                """,
                (
                    properties["city_id"],
                    properties["name"],
                    _polygon_wkt(feature["geometry"]),
                    properties["source"],
                ),
            )

        for feature in wards:
            properties = feature["properties"]
            cur.execute(
                """
                INSERT INTO wards (ward_id, name, zone, city_id, geom, source)
                VALUES (%s, %s, %s, %s, ST_GeogFromText(%s), %s)
                ON CONFLICT (ward_id) DO UPDATE
                SET name = EXCLUDED.name,
                    zone = EXCLUDED.zone,
                    city_id = EXCLUDED.city_id,
                    geom = EXCLUDED.geom,
                    source = EXCLUDED.source
                """,
                (
                    properties["ward_id"],
                    properties["name"],
                    properties.get("zone"),
                    properties["city_id"],
                    _polygon_wkt(feature["geometry"]),
                    properties["source"],
                ),
            )

    log.info("reference geography loaded", extra={"cities": len(cities), "wards": len(wards)})
    return len(cities), len(wards)


def _point_wkt(geometry: dict[str, Any]) -> str:
    if geometry["type"] != "Point":
        raise ValueError(f"expected a Point, got {geometry['type']!r}")
    lon, lat = geometry["coordinates"]
    return f"POINT({lon} {lat})"


def load_poi(path: pathlib.Path = POI_FILE) -> int:
    """Load the synthetic POI FeatureCollection and the ward road-class table.

    Returns the number of POI points written. Both are reference data, not
    pipeline output, so this is idempotent the same way `load()` is.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    features = document["features"]

    with transaction() as conn, conn.cursor() as cur:
        for feature in features:
            properties = feature["properties"]
            cur.execute(
                """
                INSERT INTO poi (poi_id, kind, name, geom, source)
                VALUES (%s, %s, %s, ST_GeogFromText(%s), %s)
                ON CONFLICT (poi_id) DO UPDATE
                SET kind = EXCLUDED.kind,
                    name = EXCLUDED.name,
                    geom = EXCLUDED.geom,
                    source = EXCLUDED.source
                """,
                (
                    properties["poi_id"],
                    properties["kind"],
                    properties["name"],
                    _point_wkt(feature["geometry"]),
                    properties["source"],
                ),
            )

        for ward_id, road_class in WARD_ROAD_CLASS.items():
            cur.execute(
                """
                INSERT INTO ward_road_class (ward_id, dominant_road_class, source)
                VALUES (%s, %s, %s)
                ON CONFLICT (ward_id) DO UPDATE
                SET dominant_road_class = EXCLUDED.dominant_road_class,
                    source = EXCLUDED.source
                """,
                (ward_id, road_class, "hand-labelled-fixture"),
            )

    log.info(
        "poi reference data loaded",
        extra={"poi": len(features), "ward_road_class": len(WARD_ROAD_CLASS)},
    )
    return len(features)


def main() -> int:
    configure_logging()
    cities, wards = load()
    poi = load_poi()
    print(f"loaded {cities} city boundary/boundaries and {wards} wards")
    print("note: simplified fixture geometry, not official BBMP boundaries")
    print(f"loaded {poi} POI points and {len(WARD_ROAD_CLASS)} ward road classes")
    print("note: synthetic placeholder POI data, not official BBMP/OSM data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
