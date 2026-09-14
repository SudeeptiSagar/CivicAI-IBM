"""The database schema against PRD section 10, and the migration runner itself.

The enum-alignment tests matter most. Every enumerated column exists in two
places — the JSON Schema under `schemas/` and a CHECK constraint in SQL — and
nothing but a test stops them drifting apart. Drift would mean Sentinel accepts
a value the database rejects, or worse, the reverse.

Skipped when Postgres is unreachable.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest

from common.db import connect, transaction
from common.schemas import load_all
from db import migrate
from tests.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]

#: Every table PRD section 10 names, plus the four documented additions.
PRD_TABLES = {
    "reports",
    "incidents",
    "super_incidents",
    "evidence",
    "resolutions",
    "verification_results",
    "agent_runs",
}
ADDED_TABLES = {"departments", "messages", "quarantine", "sentinel_alerts"}


def _tables(conn: psycopg.Connection[dict[str, Any]]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {str(row["tablename"]) for row in cur.fetchall()}


def _check_values(conn: psycopg.Connection[dict[str, Any]], table: str, column: str) -> set[str]:
    """Values allowed by the CHECK constraint covering `table.column`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_constraintdef(c.oid) AS definition
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = %s AND c.contype = 'c'
            """,
            (table,),
        )
        definitions = [str(row["definition"]) for row in cur.fetchall()]

    for definition in definitions:
        if re.search(rf"\b{re.escape(column)}\b", definition) and "ARRAY[" in definition:
            return set(re.findall(r"'([^']+)'::text", definition))
    return set()


# -- structure -----------------------------------------------------------


def test_every_prd_table_exists(database: None) -> None:
    with connect() as conn:
        assert _tables(conn) >= PRD_TABLES


def test_documented_additional_tables_exist(database: None) -> None:
    """Four tables beyond PRD section 10, each justified in docs/data-model.md."""
    with connect() as conn:
        assert _tables(conn) >= ADDED_TABLES


def test_postgis_and_pgvector_are_both_installed(database: None) -> None:
    """PRD section 6.2 wants geo queries and embedding search in one store."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension")
        installed = {str(row["extname"]) for row in cur.fetchall()}
    assert {"postgis", "vector"} <= installed


def test_reports_geometry_is_geography_4326(database: None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT type, srid FROM geography_columns "
            "WHERE f_table_name = 'reports' AND f_geography_column = 'geom'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row["type"] == "Point"
    assert row["srid"] == 4326


def test_embedding_column_is_768_dimensional(database: None) -> None:
    """PRD section 10 fixes the embedding at 768 dimensions."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT format_type(atttypid, atttypmod) AS type FROM pg_attribute "
            "WHERE attrelid = 'reports'::regclass AND attname = 'embedding'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row["type"] == "vector(768)"


def test_spatial_and_vector_indexes_exist(database: None) -> None:
    """A2 dedup does geo and cosine lookups on every report (PRD section 7/A2)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'reports'")
        definitions = " ".join(str(row["indexdef"]) for row in cur.fetchall())

    assert "USING gist" in definitions
    assert "USING hnsw" in definitions


def test_departments_are_seeded(database: None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM departments")
        row = cur.fetchone()
    assert row is not None
    assert row["n"] == 6


# -- enum alignment with schemas/ ----------------------------------------


def test_department_registry_matches_the_schema_enum(database: None) -> None:
    """PRD 7/A5 makes "department in registry" an invariant; the registry and
    the contract have to agree on what the registry contains."""
    expected = set(load_all()["envelope.v1"]["$defs"]["department"]["enum"])

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT department_id FROM departments")
        actual = {str(row["department_id"]) for row in cur.fetchall()}

    assert actual == expected


@pytest.mark.parametrize(
    ("table", "column", "schema_key", "pointer"),
    [
        ("verification_results", "layer", "verification.results.v1", "layer"),
        ("verification_results", "verdict", "verification.results.v1", "verdict"),
        ("evidence", "kind", "evidence.attached.v1", "kind"),
        ("evidence", "verdict", "evidence.attached.v1", "verdict"),
        ("sentinel_alerts", "kind", "sentinel.alert.v1", "kind"),
        ("sentinel_alerts", "severity", "sentinel.alert.v1", "severity"),
    ],
)
def test_check_constraints_match_the_schema_enums(
    database: None, table: str, column: str, schema_key: str, pointer: str
) -> None:
    schema = load_all()[schema_key]
    expected = set(schema["properties"]["payload"]["properties"][pointer]["enum"])

    with connect() as conn:
        actual = _check_values(conn, table, column)

    assert actual == expected, f"{table}.{column} drifted from {schema_key}"


def test_incident_status_matches_the_schema_enum(database: None) -> None:
    expected = set(
        load_all()["incidents.updated.v1"]["properties"]["payload"]["properties"]["status"]["enum"]
    )
    with connect() as conn:
        actual = _check_values(conn, "incidents", "status")
    assert actual == expected


def test_priority_band_matches_the_schema_enum(database: None) -> None:
    expected = set(
        load_all()["incidents.prioritized.v1"]["properties"]["payload"]["properties"][
            "priority_band"
        ]["enum"]
    )
    with connect() as conn:
        actual = _check_values(conn, "incidents", "priority_band")
    assert actual == expected


# -- invariants enforced in SQL ------------------------------------------


def test_score_without_breakdown_is_rejected(clean_db: None) -> None:
    """PRD 8.1/A4: a score with no factor breakdown is not a valid score."""
    with pytest.raises(psycopg.errors.CheckViolation), transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO incidents (
                incident_id, title, category, centroid, first_reported_at,
                last_reported_at, priority_score, factor_breakdown
            ) VALUES (
                gen_random_uuid(), 'x', 'pothole',
                ST_GeogFromText('POINT(77.61 12.93)'), now(), now(), 93.0, NULL
            )
            """
        )


def test_verified_resolution_needs_grounds(clean_db: None) -> None:
    """PRD 7/A6: no verified state without two confirmations or a visual match."""
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO incidents (incident_id, title, category, centroid,
                                   first_reported_at, last_reported_at)
            VALUES (gen_random_uuid(), 'x', 'pothole',
                    ST_GeogFromText('POINT(77.61 12.93)'), now(), now())
            RETURNING incident_id
            """
        )
        row = conn.execute("SELECT incident_id FROM incidents LIMIT 1").fetchone()
        assert row is not None
        incident_id = row["incident_id"]

    with pytest.raises(psycopg.errors.CheckViolation), transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO resolutions (
                resolution_id, incident_id, claimed_by, claimed_at,
                citizen_confirmations, citizen_disputes, visual_verdict, final_status
            ) VALUES (gen_random_uuid(), %s, 'officer:1', now(), 0, 0, 'inconclusive', 'verified')
            """,
            (incident_id,),
        )


def test_confidence_outside_the_unit_interval_is_rejected(clean_db: None) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (
                message_id, trace_id, correlation_id, topic, schema_version,
                emitted_at, producer_agent, producer_version, confidence,
                rationale, envelope
            ) VALUES (gen_random_uuid(), gen_random_uuid(), 'c', 'reports.ingested',
                      '1.0.0', now(), 'A0', '1.0.0', 1.5, 'r', '{}'::jsonb)
            """
        )


def test_one_verdict_per_message_and_layer(clean_db: None) -> None:
    """Re-verifying a message is idempotent, not additive."""
    with transaction() as conn, conn.cursor() as cur:
        for _ in range(2):
            cur.execute(
                """
                INSERT INTO verification_results (
                    verdict_id, message_id, trace_id, topic, agent, layer, verdict
                ) VALUES (
                    gen_random_uuid(),
                    '00000000-0000-7000-8000-000000000001',
                    '00000000-0000-7000-8000-000000000002',
                    'reports.ingested', 'A0', 'L1', 'pass'
                )
                ON CONFLICT (message_id, layer) DO NOTHING
                """
            )
        cur.execute("SELECT count(*) AS n FROM verification_results")
        row = cur.fetchone()

    assert row is not None
    assert row["n"] == 1


# -- the runner ----------------------------------------------------------


def test_migrations_are_discovered_in_order() -> None:
    versions = [m.version for m in migrate.discover()]
    assert versions == sorted(versions)
    assert "0001_init" in versions


def test_nothing_pending_after_up(database: None) -> None:
    done, todo = migrate.status()
    assert "0001_init" in done
    assert todo == []


def test_up_is_idempotent(database: None) -> None:
    assert migrate.up() == []


def test_editing_an_applied_migration_is_an_error(database: None) -> None:
    """Silently ignoring a changed file would put environments out of step."""
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = '0001_init'"
        )

    try:
        with connect() as conn, pytest.raises(migrate.MigrationDriftError, match="0001_init"):
            migrate.pending(conn)
    finally:
        real = next(m for m in migrate.discover() if m.version == "0001_init")
        with transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE version = '0001_init'",
                (real.checksum,),
            )
