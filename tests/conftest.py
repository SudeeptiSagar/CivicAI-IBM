"""Shared fixtures.

Integration tests need the compose stack. Rather than fail when it is absent,
they skip — so `pytest` stays green on a laptop with nothing running, while CI
and a developer with `docker compose up` get the real coverage. A skipped
integration test is reported as skipped, never as passed.

Isolation matters more than speed here:

* Postgres work happens in a separate `civicai_test` database, created and
  migrated on demand, so a test run can never truncate the dev data sitting in
  `civicai`.
* Redis work happens on database index 15, flushed between tests, so a test can
  never consume a stream the running agents are working on.

Both are pointed at by environment variables set at import time, before
anything calls `settings()`, because that call is cached for the process.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from typing import Any

import pytest

# --- redirect configuration before anything reads it -------------------------

TEST_DB_NAME = "civicai_test"
TEST_REDIS_INDEX = 15

_ADMIN_URL = os.environ.get(
    "CIVICAI_TEST_ADMIN_URL", "postgresql://civicai:civicai@localhost:5432/postgres"
)
_TEST_DB_URL = f"postgresql://civicai:civicai@localhost:5432/{TEST_DB_NAME}"
_TEST_REDIS_URL = f"redis://localhost:6379/{TEST_REDIS_INDEX}"

os.environ["CIVICAI_DATABASE_URL"] = _TEST_DB_URL
os.environ["CIVICAI_REDIS_URL"] = _TEST_REDIS_URL
os.environ.setdefault("CIVICAI_ENV", "ci")

# Object store: the compose MinIO, with its dev credentials. Media tests use a
# key prefix of their own so they never collide with stored report media.
os.environ.setdefault("CIVICAI_S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("CIVICAI_S3_BUCKET", "civicai-media")
os.environ.setdefault("CIVICAI_S3_ACCESS_KEY", "civicai")
os.environ.setdefault("CIVICAI_S3_SECRET_KEY", "civicai-dev-secret")

# Tables the pipeline writes. Truncated between integration tests.
#
# `intake_attempts` matters more than it looks: leaving it behind lets A0's
# per-device rate limiter carry state across tests, and a later test fails with
# a rate-limit rejection that has nothing to do with what it was checking.
#
# Reference geography (`wards`, `city_boundary`) and `departments` are NOT here:
# they are seeded data, not test output.
_MUTABLE_TABLES = (
    "intake_attempts",
    "handler_results",
    "quarantine",
    "verification_results",
    "sentinel_alerts",
    "agent_runs",
    "messages",
    "resolutions",
    "evidence",
    "reports",
    "incidents",
    "super_incidents",
)


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def postgres_available() -> bool:
    return _reachable("localhost", 5432)


def redis_available() -> bool:
    return _reachable("localhost", 6379)


def minio_available() -> bool:
    return _reachable("localhost", 9000)


requires_postgres = pytest.mark.skipif(
    not postgres_available(), reason="Postgres not reachable - run `docker compose up -d postgres`"
)
requires_redis = pytest.mark.skipif(
    not redis_available(), reason="Redis not reachable - run `docker compose up -d redis`"
)
requires_minio = pytest.mark.skipif(
    not minio_available(), reason="MinIO not reachable - run `docker compose up -d minio`"
)


# --- Postgres ----------------------------------------------------------------


@pytest.fixture(scope="session")
def database() -> Iterator[None]:
    """Create and migrate the test database once per session."""
    if not postgres_available():
        pytest.skip("Postgres not reachable")

    import psycopg

    from db import migrate

    with psycopg.connect(_ADMIN_URL, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')

    migrate.up()
    yield

    from common import db

    db.close_pool()


@pytest.fixture
def clean_db(database: None) -> Iterator[None]:
    """Empty every mutable table before the test runs.

    `departments` is left alone: it is reference data seeded by the migration,
    not test output.
    """
    from common.db import transaction

    with transaction() as conn, conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(_MUTABLE_TABLES)} RESTART IDENTITY CASCADE")
    yield


# --- Redis -------------------------------------------------------------------


@pytest.fixture
def redis_bus(clean_redis: None) -> Iterator[Any]:
    """A `RedisBus` pointed at the flushed test database index."""
    from bus.redis import RedisBus

    bus = RedisBus(_TEST_REDIS_URL)
    yield bus
    bus.close()


@pytest.fixture
def clean_redis() -> Iterator[None]:
    """Flush the test Redis index before the test runs."""
    if not redis_available():
        pytest.skip("Redis not reachable")

    from redis import Redis

    client = Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
    client.flushdb()
    yield
    client.flushdb()
    client.close()


# --- in-memory ---------------------------------------------------------------


@pytest.fixture
def memory_bus() -> Any:
    """An in-memory bus, for tests that need no infrastructure."""
    from bus.memory import InMemoryBus

    return InMemoryBus()
