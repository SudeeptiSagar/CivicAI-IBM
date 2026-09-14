"""Handler-side idempotency (PRD section 9.4).

The bus is at-least-once, so every handler must be idempotent: a repeat of
`(correlation_id, topic, schema_version, producer.version)` is a no-op that
returns the prior result.

The in-memory store here is enough for P0 and for tests. P1 replaces it with a
Postgres-backed store so idempotency survives a process restart; both satisfy
the `IdempotencyStore` protocol, so handler code does not change.
"""

from __future__ import annotations

from typing import Any, Protocol

from common.envelope import Envelope

__all__ = ["IdempotencyStore", "InMemoryIdempotencyStore", "key_for"]


def key_for(envelope: Envelope) -> str:
    """The dedup key for `envelope`."""
    return envelope.idempotency_key()


class IdempotencyStore(Protocol):
    """Records what a handler already did, keyed by `key_for()`."""

    def seen(self, key: str) -> bool:
        """True if this key was already handled."""
        ...

    def result_for(self, key: str) -> Any:
        """The result recorded for `key`.

        Raises:
            KeyError: if the key was never recorded.
        """
        ...

    def record(self, key: str, result: Any) -> None:
        """Record the outcome of handling `key`. First write wins."""
        ...


class InMemoryIdempotencyStore:
    """Process-local store. Loses state on restart — P1 swaps in Postgres."""

    def __init__(self) -> None:
        self._results: dict[str, Any] = {}

    def seen(self, key: str) -> bool:
        return key in self._results

    def result_for(self, key: str) -> Any:
        return self._results[key]

    def record(self, key: str, result: Any) -> None:
        # First write wins: a redelivery must not overwrite the original
        # outcome, or replay would silently change history.
        self._results.setdefault(key, result)

    def clear(self) -> None:
        """Drop all records. Tests only."""
        self._results.clear()
