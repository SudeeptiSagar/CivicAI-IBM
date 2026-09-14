"""Load city and ward polygons into Postgres.

    python -m scripts.load_reference_data

Idempotent: re-running replaces the geometry for ids that already exist, so it
is safe to run after editing the GeoJSON.

The bundled geometry is a simplified fixture, not official BBMP data. See the
`_disclaimer` field in data/reference/bengaluru.geojson.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

from common.db import transaction
from common.logging import configure_logging, get_logger

__all__ = ["REFERENCE_FILE", "load"]

log = get_logger(__name__)

REFERENCE_FILE = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "reference" / "bengaluru.geojson"
)


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


def main() -> int:
    configure_logging()
    cities, wards = load()
    print(f"loaded {cities} city boundary/boundaries and {wards} wards")
    print("note: simplified fixture geometry, not official BBMP boundaries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
