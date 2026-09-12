"""Uploads a validated slide + its metadata to GCS.

Object layout follows whatever the metadata's own `slide_paths` field
already declares (e.g. `Collection_PART/Frontal_Hirano/45122.svs`), rather
than imposing a new convention — real brain-bank buckets already have their
own per-institution layouts in production, and this tool should slot into
that, not fight it. If `slide_paths` isn't already relative to the bucket
root, pass an explicit `object_key` instead.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path

from google.cloud import storage

from ..checksum import compute_crc32c_base64
from ..schema import Schema, load_schema


@dataclass
class UploadResult:
    object_uri: str
    skipped: bool  # True if an identical object already existed (idempotent no-op)
    crc32c: str


def _object_key_for(record: dict, local_path: Path) -> str:
    declared = record.get("slide_paths")
    if declared and not Path(declared).is_absolute():
        return declared
    return local_path.name


def upload_slide(
    *,
    bucket: storage.Bucket,
    slide_path: Path,
    record: dict,
    schema: Schema | None = None,
    object_key: str | None = None,
    extra_files: list[Path] | None = None,
    needs_review: list[str] | None = None,
) -> UploadResult:
    """Uploads one validated slide (already passed integrity + metadata
    validation) plus a co-located metadata.json. Skips re-upload if an
    object with a matching CRC32C already exists at the destination.

    The local CRC32C is only precomputed (a full sequential read of the
    file) when something already exists at the destination and needs to be
    compared against — for a first-time upload there's nothing to compare
    to, so we skip straight to `upload_from_filename(..., checksum="crc32c")`,
    which computes and verifies the checksum as a byproduct of the transfer
    itself and refreshes `blob.crc32c` from the server's response. Avoids
    reading a multi-GB slide twice for every new upload.
    """
    schema = schema or load_schema()
    slide_path = Path(slide_path)
    key = object_key or _object_key_for(record, slide_path)

    existing = bucket.get_blob(key)
    if existing is not None:
        local_crc32c = compute_crc32c_base64(slide_path)
        if existing.crc32c == local_crc32c:
            return UploadResult(object_uri=f"gs://{bucket.name}/{key}", skipped=True, crc32c=local_crc32c)

    blob = bucket.blob(key)
    blob.metadata = {
        "schema_version": schema.version,
        "validated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "participant_id": record.get("participant_id", ""),
        "stain_type": record.get("stain_type", ""),
    }
    if needs_review:
        # Flagged here, not just in the CLI/report output, so it isn't lost
        # once the record leaves this run — queryable via `gcloud storage`
        # without needing to re-run validation or dig up the batch report.
        blob.metadata["needs_review"] = ",".join(needs_review)
    blob.upload_from_filename(str(slide_path), checksum="crc32c")

    local_crc32c = blob.crc32c  # already client-verified against the server during the transfer above
    blob.metadata["client_checksum_crc32c"] = local_crc32c
    blob.patch()

    metadata_key = str(Path(key).with_name(Path(key).stem + ".metadata.json"))
    bucket.blob(metadata_key).upload_from_string(
        json.dumps(record, indent=2), content_type="application/json"
    )

    for extra in extra_files or []:
        extra_key = f"{Path(key).parent}/{Path(key).stem}/{extra.name}" if Path(key).parent != Path(".") else extra.name
        bucket.blob(extra_key).upload_from_filename(str(extra))

    return UploadResult(object_uri=f"gs://{bucket.name}/{key}", skipped=False, crc32c=local_crc32c)
