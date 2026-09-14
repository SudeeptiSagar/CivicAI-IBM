"""Migration runner.

Plain numbered .sql files applied in order, tracked in `schema_migrations`.
Chosen over Alembic deliberately: this schema is mostly PostGIS and pgvector
DDL, which Alembic's autogenerate does not model well, and a hand-written .sql
file is the clearest possible record of what shipped.

    python -m db.migrate up        apply everything pending
    python -m db.migrate status    show applied and pending

An already-applied file that changes on disk is a hard error rather than a
silent no-op: editing history would put environments out of step with no
signal. Write a new migration instead.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
from dataclasses import dataclass

from psycopg import Connection

from common.db import connect
from common.logging import configure_logging, get_logger

__all__ = ["Migration", "applied", "discover", "pending", "status", "up"]

log = get_logger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True, slots=True)
class Migration:
    """One .sql file on disk."""

    version: str
    path: pathlib.Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


class MigrationDriftError(RuntimeError):
    """An applied migration's file no longer matches what was applied."""


def discover() -> list[Migration]:
    """Every migration file, in version order."""
    return [
        Migration(version=path.stem, path=path) for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    ]


def _ensure_bootstrap(conn: Connection[dict[str, object]]) -> None:
    with conn.cursor() as cur:
        cur.execute(_BOOTSTRAP)
    conn.commit()


def applied(conn: Connection[dict[str, object]]) -> dict[str, str]:
    """Version to checksum for everything already applied."""
    _ensure_bootstrap(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
        return {str(row["version"]): str(row["checksum"]) for row in cur.fetchall()}


def pending(conn: Connection[dict[str, object]]) -> list[Migration]:
    """Migrations not yet applied, in order.

    Raises:
        MigrationDriftError: if an applied migration's file has changed.
    """
    done = applied(conn)
    todo: list[Migration] = []

    for migration in discover():
        recorded = done.get(migration.version)
        if recorded is None:
            todo.append(migration)
        elif recorded != migration.checksum:
            raise MigrationDriftError(
                f"{migration.version} was applied with checksum {recorded[:12]} but the file "
                f"now hashes to {migration.checksum[:12]}. Add a new migration instead of "
                f"editing an applied one."
            )

    return todo


def up(conn: Connection[dict[str, object]] | None = None) -> list[str]:
    """Apply every pending migration. Returns the versions applied."""
    if conn is None:
        with connect() as owned:
            return up(owned)

    applied_now: list[str] = []

    for migration in pending(conn):
        log.info("applying migration", extra={"version": migration.version})
        # Each migration is one transaction: it lands whole or not at all.
        with conn.cursor() as cur:
            cur.execute(migration.sql)
            cur.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (migration.version, migration.checksum),
            )
        conn.commit()
        applied_now.append(migration.version)

    if not applied_now:
        log.info("no pending migrations")

    return applied_now


def status() -> tuple[list[str], list[str]]:
    """(applied versions, pending versions)."""
    with connect() as conn:
        done = applied(conn)
        return sorted(done), [m.version for m in pending(conn)]


def main(argv: list[str]) -> int:
    configure_logging()
    command = argv[1] if len(argv) > 1 else "up"

    if command == "up":
        versions = up()
        print(f"applied: {', '.join(versions) if versions else '(nothing pending)'}")
        return 0

    if command == "status":
        done, todo = status()
        print(f"applied ({len(done)}): {', '.join(done) or '-'}")
        print(f"pending ({len(todo)}): {', '.join(todo) or '-'}")
        return 0

    print(f"unknown command {command!r}; expected 'up' or 'status'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
