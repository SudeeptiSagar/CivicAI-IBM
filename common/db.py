"""Postgres access (PRD section 6.2).

All durable truth lives here (PRD section 6.1). Agents own their own tables and
never write to another agent's — that rule is enforced by review and by the
Sentinel invariants in P4, not by grants, but the repository functions below
are split by owner to make a violation obvious in a diff.

Connections come from a pool. `connect()` hands out one connection with
autocommit off; `transaction()` wraps a unit of work that must land whole.
"""

from __future__ import annotations

import atexit
import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from common.config import settings

__all__ = ["Json", "close_pool", "connect", "healthy", "pool", "transaction"]

Row = dict[str, Any]


class Json:
    """Marks a value for JSONB adaptation.

    psycopg will not guess that a dict belongs in a jsonb column, so call sites
    wrap explicitly: `Json({"a": 1})`.
    """

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value

    def dumps(self) -> str:
        return json.dumps(self.value, default=str)


@lru_cache(maxsize=1)
def pool() -> ConnectionPool[psycopg.Connection[Row]]:
    """Process-wide connection pool.

    `open=True` connects lazily on first use rather than at import, so a module
    import does not fail merely because Postgres is not up yet.
    """
    created: ConnectionPool[psycopg.Connection[Row]] = ConnectionPool(
        conninfo=settings().database_url,
        min_size=1,
        max_size=10,
        open=True,
        kwargs={"row_factory": dict_row, "autocommit": False},
        name="civicai",
    )
    # Without this, a short-lived process (the migration runner, a script) exits
    # while pool workers are still parked and psycopg logs a shutdown warning.
    atexit.register(close_pool)
    return created


@contextmanager
def connect() -> Iterator[psycopg.Connection[Row]]:
    """A pooled connection. Caller commits."""
    with pool().connection() as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[psycopg.Connection[Row]]:
    """A pooled connection wrapped in a transaction.

    Commits on clean exit, rolls back on any exception.
    """
    with pool().connection() as conn, conn.transaction():
        yield conn


def close_pool() -> None:
    """Shut the pool down. Process exit and tests."""
    if pool.cache_info().currsize:
        pool().close()
        pool.cache_clear()


def healthy() -> bool:
    """True if Postgres answers. Used by the health endpoint."""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None
    except Exception:
        # Health checks report, they do not raise: an unreachable database is a
        # status to surface, not a crash.
        return False
