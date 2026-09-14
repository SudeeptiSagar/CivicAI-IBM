# Citizen media handling

PRD section 15 lists "PII in citizen media" as a high-impact risk and asks for
three controls: strip EXIF except the geotag, blur faces and plates on ingest,
and apply a retention policy to raw media.

This document records which of those exist.

---

## Implemented: EXIF stripping

`common/storage.py` strips metadata by **re-encoding the image**, not by
deleting tags. Pixels are copied into a fresh image object and saved, so no
metadata segment survives — including ones the library does not parse and we
therefore could not have thought to delete.

The geotag is read out first and returned on `StoredMedia.exif_location`,
because PRD section 7/A0 wants it: it corroborates the device GPS, and A0 falls
back to it when a submission arrives with no usable fix.

`tests/test_media.py` verifies this against the **stored bytes**, not the
parsed tags — a photographer's name surviving in a segment Pillow ignores would
still be a leak, and a test that only checked `getexif()` would not see it.

## Implemented: upload validation

* 10 MB cap. The PRD requires size validation without naming a limit; this is
  well above a phone photo and well below anything that would stall intake.
* Allow-listed content types: JPEG, PNG and WebP for images; common audio
  types for voice notes.
* **Bytes are re-decoded, not trusted.** A declared content type is how a
  malicious upload gets past a naive check, so `store_image` opens and decodes
  the payload and rejects anything that is not the image it claims to be.

## NOT implemented: face and plate blurring

**This is an open PII gap, not an oversight.**

PRD section 15 asks for faces and number plates to be blurred on ingest. That
needs a detector — a face/plate detection model — and no vision provider is
configured in this build (PRD open question 2). Writing a blur step that did
not actually detect anything would be worse than not having one, because it
would look like the control was in place.

What this means today: **a photo of a pothole that happens to include a
bystander or a parked car is stored with that person and that plate legible.**

`tests/test_media.py::test_face_and_plate_blurring_is_not_implemented` asserts
the absence deliberately, so the gap stays visible and the test fails the
moment blurring is added — which is the prompt to replace it with real
coverage.

Closing it needs a detector behind `common/llm/provider.py` (a vision
capability), then a blur step in `store_image` before the re-encode. It belongs
with the vision work, and should be treated as a blocker for any deployment
handling real citizen photographs.

## NOT implemented: retention policy

PRD section 15 asks for a retention policy on raw media. Nothing deletes
anything today; blobs accumulate in the bucket indefinitely.

This needs a decision the PRD does not make — how long media is kept, whether
retention differs for verified and unverified incidents, and whether deletion
is hard or a lifecycle rule on the bucket. Worth settling before real data.

## Not applicable: audio sanitisation

Audio is stored as received. There is no audio equivalent of EXIF stripping
that does not involve re-encoding through a codec this build does not ship, and
container-level metadata in a voice note recorded by the PWA is not a
comparable exposure. If a future client permits file upload rather than
in-app recording, this needs revisiting.

---

## Summary

| Control | PRD section 15 | Status |
|---|---|---|
| Strip EXIF except geotag | required | **Implemented**, verified against stored bytes |
| Validate media type and size | PRD 7/A0 | **Implemented**, content sniffed not trusted |
| Blur faces and plates | required | **Not implemented** — no detector available |
| Retention policy on raw media | required | **Not implemented** — policy undecided |
