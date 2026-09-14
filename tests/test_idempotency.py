"""Handler-side idempotency (PRD section 9.4)."""

from __future__ import annotations

from common.envelope import Envelope
from common.idempotency import InMemoryIdempotencyStore
from tests.factories import envelope_for


def _store() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


def test_unseen_key_is_not_seen() -> None:
    assert not _store().seen("anything")


def test_recorded_key_is_seen() -> None:
    store = _store()
    store.record("k", "result")
    assert store.seen("k")
    assert store.result_for("k") == "result"


def test_first_write_wins() -> None:
    """A redelivery must not overwrite the original outcome, or replaying the
    stream would silently rewrite history."""
    store = _store()
    store.record("k", "original")
    store.record("k", "second attempt")
    assert store.result_for("k") == "original"


def test_result_for_unknown_key_raises() -> None:
    store = _store()
    try:
        store.result_for("missing")
    except KeyError:
        return
    raise AssertionError("expected KeyError for an unrecorded key")


def test_keys_derive_from_the_envelope() -> None:
    envelope = Envelope.from_dict(envelope_for("reports.ingested"))
    store = _store()
    store.record(envelope.idempotency_key(), "done")
    assert store.seen(envelope.idempotency_key())


def test_different_topics_do_not_collide() -> None:
    ingested = Envelope.from_dict(envelope_for("reports.ingested"))
    understood = Envelope.from_dict(
        envelope_for("reports.understood", correlation_id=ingested.correlation_id)
    )
    assert ingested.idempotency_key() != understood.idempotency_key()


def test_clear_empties_the_store() -> None:
    store = _store()
    store.record("k", "v")
    store.clear()
    assert not store.seen("k")
