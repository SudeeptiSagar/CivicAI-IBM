"""Geospatial lookups (PRD section 7/A0).

PostGIS does the work; this module is the small typed surface the agents use so
no agent writes raw spatial SQL.

Geometry is `GEOGRAPHY(..., 4326)` throughout, which means `ST_DWithin` and
`ST_Distance` take and return metres directly — the unit A2's category radii
are expressed in (PRD section 7/A2).
"""

from __future__ import annotations

from dataclasses import dataclass

from common.db import connect

__all__ = ["Point", "Ward", "distance_m", "inside_city", "point_wkt", "ward_for"]


@dataclass(frozen=True, slots=True)
class Point:
    """A WGS-84 coordinate."""

    lat: float
    lon: float

    def wkt(self) -> str:
        """PostGIS takes longitude first."""
        return f"POINT({self.lon} {self.lat})"


@dataclass(frozen=True, slots=True)
class Ward:
    ward_id: str
    name: str
    zone: str | None


def point_wkt(lat: float, lon: float) -> str:
    return Point(lat, lon).wkt()


def inside_city(lat: float, lon: float, city_id: str = "BLR") -> bool:
    """True if the point falls inside the city polygon.

    PRD section 8.1 makes this an A0 invariant. Returns False when no boundary
    is loaded: refusing everything is the safe failure for a check whose whole
    job is to refuse, and the loader script is a documented setup step.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ST_Covers(geom, ST_GeogFromText(%s)) AS inside "
            "FROM city_boundary WHERE city_id = %s",
            (point_wkt(lat, lon), city_id),
        )
        row = cur.fetchone()
    return bool(row["inside"]) if row else False


def ward_for(lat: float, lon: float) -> Ward | None:
    """The ward containing the point, or None if it falls outside every ward.

    None is a real answer, not an error: the simplified fixture geometry covers
    only part of the city, and a report between two wards must not be silently
    assigned to one of them.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ward_id, name, zone FROM wards "
            "WHERE ST_Covers(geom, ST_GeogFromText(%s)) LIMIT 1",
            (point_wkt(lat, lon),),
        )
        row = cur.fetchone()

    if row is None:
        return None
    return Ward(ward_id=str(row["ward_id"]), name=str(row["name"]), zone=row["zone"])


def distance_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ST_Distance(ST_GeogFromText(%s), ST_GeogFromText(%s)) AS d",
            (a.wkt(), b.wkt()),
        )
        row = cur.fetchone()
    return float(row["d"]) if row else float("inf")
