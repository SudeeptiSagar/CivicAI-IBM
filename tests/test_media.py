"""Citizen media handling (PRD sections 7/A0 and 15).

EXIF stripping is a privacy control, not a nicety, so these tests check the
stored bytes rather than trusting the code path. Needs MinIO.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from common.storage import (
    MAX_MEDIA_BYTES,
    MediaRejected,
    fetch,
    store_audio,
    store_image,
)
from tests.conftest import requires_minio

pytestmark = [pytest.mark.integration, requires_minio]

TEST_PREFIX = "tests"


def _jpeg(size: tuple[int, int] = (48, 32), colour: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def _jpeg_with_exif(lat_dms: tuple[int, int, float], lon_dms: tuple[int, int, float]) -> bytes:
    """A JPEG carrying a GPS tag plus identifying camera metadata."""
    image = Image.new("RGB", (48, 32), "blue")
    exif = image.getexif()

    exif[0x010F] = "AcmePhone"  # Make
    exif[0x0110] = "AcmePhone 12 Pro"  # Model
    exif[0x013B] = "Priya Sharma"  # Artist - the PII that must not survive
    exif[0x8298] = "(c) Priya Sharma"  # Copyright

    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = lat_dms
    gps[3] = "E"
    gps[4] = lon_dms

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


# -- validation ----------------------------------------------------------


def test_valid_jpeg_is_stored() -> None:
    stored = store_image(_jpeg(), "image/jpeg", prefix=TEST_PREFIX)

    assert stored.key.startswith(f"{TEST_PREFIX}/")
    assert stored.size_bytes > 0
    assert fetch(stored.key)


def test_empty_payload_is_rejected() -> None:
    with pytest.raises(MediaRejected) as raised:
        store_image(b"", "image/jpeg")
    assert raised.value.reason_code == "invalid_media"


def test_oversized_payload_is_rejected() -> None:
    with pytest.raises(MediaRejected) as raised:
        store_image(b"x" * (MAX_MEDIA_BYTES + 1), "image/jpeg")
    assert raised.value.reason_code == "media_too_large"


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises(MediaRejected) as raised:
        store_image(_jpeg(), "image/svg+xml")
    assert raised.value.reason_code == "invalid_media"


def test_bytes_that_do_not_match_the_declared_type_are_rejected() -> None:
    """A declared content type is how a malicious upload gets past a naive
    check, so the bytes are re-decoded rather than trusted."""
    with pytest.raises(MediaRejected, match="does not decode"):
        store_image(b"GIF89a this is not a jpeg", "image/jpeg")


def test_executable_disguised_as_an_image_is_rejected() -> None:
    with pytest.raises(MediaRejected):
        store_image(b"MZ\x90\x00" + b"\x00" * 500, "image/png")


def test_audio_type_is_validated() -> None:
    with pytest.raises(MediaRejected):
        store_audio(b"fake audio bytes", "application/zip")


def test_valid_audio_is_stored() -> None:
    stored = store_audio(b"fake audio bytes", "audio/mpeg", prefix=TEST_PREFIX)
    assert fetch(stored.key) == b"fake audio bytes"


# -- EXIF stripping ------------------------------------------------------


def test_geotag_is_recovered_before_stripping() -> None:
    """PRD section 7/A0 keeps the geotag precisely so it can corroborate or
    replace a missing device fix."""
    stored = store_image(
        _jpeg_with_exif((12, 56, 4.2), (77, 36, 36.4)), "image/jpeg", prefix=TEST_PREFIX
    )

    assert stored.exif_location is not None
    lat, lon = stored.exif_location
    assert lat == pytest.approx(12.9345, abs=1e-3)
    assert lon == pytest.approx(77.6101, abs=1e-3)


def test_stored_image_carries_no_exif_at_all() -> None:
    """The privacy control itself. Camera make, model and owner name must not
    survive into the object store (PRD section 15)."""
    stored = store_image(
        _jpeg_with_exif((12, 56, 4.2), (77, 36, 36.4)), "image/jpeg", prefix=TEST_PREFIX
    )

    round_tripped = Image.open(io.BytesIO(fetch(stored.key)))

    assert dict(round_tripped.getexif()) == {}


def test_owner_name_does_not_survive_in_the_raw_bytes() -> None:
    """Checked against the bytes, not the parsed tags: a name surviving in a
    segment Pillow does not parse would still be a leak."""
    raw = _jpeg_with_exif((12, 56, 4.2), (77, 36, 36.4))
    assert b"Priya Sharma" in raw  # the fixture really does carry it

    stored = store_image(raw, "image/jpeg", prefix=TEST_PREFIX)

    assert b"Priya Sharma" not in fetch(stored.key)
    assert b"AcmePhone" not in fetch(stored.key)


def test_image_content_survives_stripping() -> None:
    """Stripping must not destroy the photograph."""
    original = Image.open(io.BytesIO(_jpeg(size=(64, 48), colour="red")))
    stored = store_image(_jpeg(size=(64, 48), colour="red"), "image/jpeg", prefix=TEST_PREFIX)

    round_tripped = Image.open(io.BytesIO(fetch(stored.key)))

    assert round_tripped.size == original.size
    assert round_tripped.mode == original.mode


def test_image_without_exif_reports_no_location() -> None:
    stored = store_image(_jpeg(), "image/jpeg", prefix=TEST_PREFIX)
    assert stored.exif_location is None


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png", "image/webp"])
def test_every_accepted_image_type_round_trips(content_type: str) -> None:
    buffer = io.BytesIO()
    fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[content_type]
    Image.new("RGB", (32, 32), "green").save(buffer, format=fmt)

    stored = store_image(buffer.getvalue(), content_type, prefix=TEST_PREFIX)

    assert Image.open(io.BytesIO(fetch(stored.key))).size == (32, 32)


# -- storage -------------------------------------------------------------


def test_keys_are_unique() -> None:
    first = store_image(_jpeg(), "image/jpeg", prefix=TEST_PREFIX)
    second = store_image(_jpeg(), "image/jpeg", prefix=TEST_PREFIX)
    assert first.key != second.key


def test_content_hash_is_recorded() -> None:
    stored = store_image(_jpeg(), "image/jpeg", prefix=TEST_PREFIX)
    assert len(stored.sha256) == 64


def test_fetching_an_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        fetch("tests/does/not/exist.jpg")


# -- the documented gap --------------------------------------------------


def test_face_and_plate_blurring_is_not_implemented() -> None:
    """PRD section 15 asks for faces and plates to be blurred on ingest. No
    detector is available in this build, so it is not done.

    This test exists to keep that gap visible rather than forgotten: it fails
    the moment blurring is added, which is the prompt to delete it and write
    real coverage. See docs/media-handling.md.
    """
    import common.storage

    assert not hasattr(common.storage, "blur_faces")
    assert not hasattr(common.storage, "blur_plates")
