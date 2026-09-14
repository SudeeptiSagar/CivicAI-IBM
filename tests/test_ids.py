"""UUIDv7 minting (PRD section 9.2)."""

from __future__ import annotations

import time

import pytest

from common.ids import new_message_id, new_trace_id, timestamp_ms_of, uuid7


def test_version_is_7() -> None:
    assert uuid7().version == 7


def test_variant_is_rfc_4122() -> None:
    assert uuid7().variant == "specified in RFC 4122"


def test_ids_are_unique() -> None:
    assert len({uuid7() for _ in range(10_000)}) == 10_000


def test_ids_are_time_ordered_across_milliseconds() -> None:
    """Time ordering is what makes a stream of ids sortable without a timestamp."""
    first = uuid7()
    time.sleep(0.005)
    second = uuid7()
    assert first < second


def test_embedded_timestamp_tracks_wall_clock() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    assert before <= timestamp_ms_of(value) <= after


def test_timestamp_extraction_rejects_other_uuid_versions() -> None:
    import uuid

    with pytest.raises(ValueError, match="expected a UUIDv7"):
        timestamp_ms_of(uuid.uuid4())


def test_message_and_trace_ids_are_uuid7() -> None:
    assert new_message_id().version == 7
    assert new_trace_id().version == 7


def test_message_and_trace_ids_are_independent() -> None:
    assert new_message_id() != new_trace_id()
