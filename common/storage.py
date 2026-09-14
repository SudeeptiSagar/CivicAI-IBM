"""Object storage for citizen media (PRD section 6.2).

S3-compatible, MinIO locally. Blobs live here; Postgres holds only the keys, so
`reports.media_keys` stays small and the database is never a file server.

Media is sanitised on the way in (PRD section 15, "PII in citizen media"):

* **EXIF is stripped except the geotag.** The geotag is the one field with
  operational value — it corroborates the device GPS — and everything else
  (camera serial, owner name, thumbnails that can survive cropping) is
  discarded by re-encoding the image rather than by deleting tags, so nothing
  survives in a segment we forgot to look at.
* **Face and plate blurring is NOT implemented.** PRD section 15 calls for it
  and it needs a detector this build does not have. This is a real, open PII
  gap, recorded in docs/media-handling.md rather than quietly skipped.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Final

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from PIL import Image

from common.config import settings
from common.ids import uuid7
from common.logging import get_logger

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

__all__ = [
    "ALLOWED_AUDIO_TYPES",
    "ALLOWED_IMAGE_TYPES",
    "MAX_MEDIA_BYTES",
    "MediaRejected",
    "StoredMedia",
    "client",
    "fetch",
    "healthy",
    "store_audio",
    "store_image",
]

log = get_logger(__name__)

#: PRD section 7/A0 validates media size without naming a limit. 10 MB is
#: comfortably above a phone photo and well below anything that would stall
#: intake.
MAX_MEDIA_BYTES: Final = 10 * 1024 * 1024

ALLOWED_IMAGE_TYPES: Final = frozenset({"image/jpeg", "image/png", "image/webp"})
ALLOWED_AUDIO_TYPES: Final = frozenset(
    {"audio/mpeg", "audio/mp4", "audio/m4a", "audio/wav", "audio/webm", "audio/ogg"}
)

#: Pillow formats we are willing to re-encode to, keyed by input content type.
_REENCODE_FORMAT: Final[dict[str, str]] = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}


class MediaRejected(ValueError):
    """The upload failed validation. Carries an A0 rejection reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class StoredMedia:
    """A blob that made it into the object store."""

    key: str
    content_type: str
    size_bytes: int
    sha256: str
    #: Geotag recovered from EXIF before stripping, if the image carried one.
    exif_location: tuple[float, float] | None = None


@lru_cache(maxsize=1)
def client() -> S3Client:
    """The S3 client. MinIO needs path-style addressing."""
    config = settings()
    return boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=config.s3_access_key,
        aws_secret_access_key=config.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )


def healthy() -> bool:
    """True if the bucket is reachable. Used by the health endpoint."""
    try:
        client().head_bucket(Bucket=settings().s3_bucket)
        return True
    except Exception:
        return False


def _key(prefix: str, extension: str) -> str:
    """Date-partitioned key, so a bucket listing stays navigable."""
    today = dt.datetime.now(dt.UTC)
    return f"{prefix}/{today:%Y/%m/%d}/{uuid7()}{extension}"


def _check_size(payload: bytes) -> None:
    if not payload:
        raise MediaRejected("invalid_media", "media payload is empty")
    if len(payload) > MAX_MEDIA_BYTES:
        raise MediaRejected(
            "media_too_large",
            f"media is {len(payload)} bytes, limit is {MAX_MEDIA_BYTES}",
        )


def _exif_location(image: Image.Image) -> tuple[float, float] | None:
    """Pull a decimal lat/lon out of EXIF GPS tags, if present."""
    try:
        exif = image.getexif()
        gps = exif.get_ifd(0x8825)  # GPSInfo
    except Exception:
        return None

    if not gps:
        return None

    try:
        lat = _dms_to_degrees(gps[2], gps[1])
        lon = _dms_to_degrees(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None

    if lat is None or lon is None:
        return None
    return (lat, lon)


def _dms_to_degrees(dms: object, ref: object) -> float | None:
    """EXIF stores GPS as degrees/minutes/seconds rationals plus a hemisphere."""
    if not isinstance(dms, tuple | list) or len(dms) != 3:
        return None

    degrees, minutes, seconds = (float(value) for value in dms)
    result = degrees + minutes / 60.0 + seconds / 3600.0

    if isinstance(ref, str) and ref.upper() in {"S", "W"}:
        result = -result
    return result


def store_image(payload: bytes, content_type: str, *, prefix: str = "reports") -> StoredMedia:
    """Validate, strip EXIF, and store one image.

    Raises:
        MediaRejected: on an unsupported type, an oversized payload, or bytes
            that do not decode as the image they claim to be.
    """
    _check_size(payload)

    if content_type not in ALLOWED_IMAGE_TYPES:
        raise MediaRejected(
            "invalid_media",
            f"content type {content_type!r} is not an accepted image type",
        )

    try:
        image = Image.open(io.BytesIO(payload))
        image.load()
    except Exception as exc:
        # A declared content type that does not match the bytes is how a
        # malicious upload gets past a naive type check.
        raise MediaRejected(
            "invalid_media", f"payload does not decode as {content_type}: {exc}"
        ) from exc

    location = _exif_location(image)

    # Re-encode through a fresh image object. Copying pixels rather than
    # deleting tags guarantees no metadata segment survives.
    stripped = Image.new(image.mode, image.size)
    stripped.putdata(list(image.getdata()))

    buffer = io.BytesIO()
    stripped.save(buffer, format=_REENCODE_FORMAT[content_type])
    cleaned = buffer.getvalue()

    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
    key = _key(prefix, extension)

    client().put_object(
        Bucket=settings().s3_bucket,
        Key=key,
        Body=cleaned,
        ContentType=content_type,
    )

    log.info(
        "image stored",
        extra={"key": key, "bytes": len(cleaned), "had_exif_gps": location is not None},
    )

    return StoredMedia(
        key=key,
        content_type=content_type,
        size_bytes=len(cleaned),
        sha256=hashlib.sha256(cleaned).hexdigest(),
        exif_location=location,
    )


def store_audio(payload: bytes, content_type: str, *, prefix: str = "reports") -> StoredMedia:
    """Validate and store one audio clip.

    Audio is stored as received: there is no audio equivalent of EXIF stripping
    that does not require re-encoding through a codec this build does not ship.
    """
    _check_size(payload)

    if content_type not in ALLOWED_AUDIO_TYPES:
        raise MediaRejected(
            "invalid_media",
            f"content type {content_type!r} is not an accepted audio type",
        )

    key = _key(prefix, ".audio")
    client().put_object(
        Bucket=settings().s3_bucket, Key=key, Body=payload, ContentType=content_type
    )

    return StoredMedia(
        key=key,
        content_type=content_type,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def fetch(key: str) -> bytes:
    """Read a stored blob.

    Raises:
        KeyError: if the object does not exist.
    """
    try:
        response = client().get_object(Bucket=settings().s3_bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            raise KeyError(key) from exc
        raise
    return response["Body"].read()
