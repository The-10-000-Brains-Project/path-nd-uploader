"""Uploads a validated slide + its metadata to GCS.

Object layout follows whatever the metadata's own `slide_paths` field
already declares (e.g. `Collection_PART/Frontal_Hirano/45122.svs`), rather
than imposing a new convention — real brain-bank buckets already have their
own per-institution layouts in production, and this tool should slot into
that, not fight it. If `slide_paths` isn't already relative to the bucket
root, pass an explicit `object_key` instead.

The actual file transfer shells out to `gcloud storage cp` rather than the
Python client's `Blob.upload_from_filename()`. That convenience wrapper
retries individual API calls, but a plain `tenacity`-style retry around it
would only restart a failed multi-GB transfer from byte zero — it has no
way to resume a genuinely interrupted upload, including surviving this
process itself being killed partway through (laptop sleep, terminal
closed). `gcloud storage cp` already solves that: it's a purpose-built,
heavily production-hardened tool with real resumable-session persistence
across process restarts, which reimplementing in pure Python would just be
worse version of. It also adds no new external dependency — `gcloud` is
already required for this tool's authentication.

Everything around the transfer (the idempotency check, custom metadata,
the metadata.json sidecar) stays on the `google-cloud-storage` Python
client, since those are cheap metadata operations, not large transfers.
Note this means BOTH of `gcloud`'s credential stores need to be set up —
`gcloud auth login` for the `gcloud storage cp` subprocess, and
`gcloud auth application-default login` for the Python client calls — they
are genuinely separate credential stores.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
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


def _gcloud_storage_cp(local_path: Path, dest_uri: str) -> None:
    result = subprocess.run(
        ["gcloud", "storage", "cp", str(local_path), dest_uri],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gcloud storage cp failed for {local_path} -> {dest_uri}: {result.stderr.strip()}")


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
    to, so we skip straight to the transfer and read the resulting crc32c
    back from GCS afterward. Avoids reading a multi-GB slide twice for
    every new upload.
    """
    schema = schema or load_schema()
    slide_path = Path(slide_path)
    key = object_key or _object_key_for(record, slide_path)
    dest_uri = f"gs://{bucket.name}/{key}"

    existing = bucket.get_blob(key)
    if existing is not None:
        local_crc32c = compute_crc32c_base64(slide_path)
        if existing.crc32c == local_crc32c:
            return UploadResult(object_uri=dest_uri, skipped=True, crc32c=local_crc32c)

    _gcloud_storage_cp(slide_path, dest_uri)

    blob = bucket.blob(key)
    blob.reload()  # picks up the crc32c gcloud storage cp's own transfer already verified
    local_crc32c = blob.crc32c
    blob.metadata = {
        "schema_version": schema.version,
        "validated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "participant_id": record.get("participant_id", ""),
        "stain_type": record.get("stain_type", ""),
        "client_checksum_crc32c": local_crc32c,
    }
    if needs_review:
        # Flagged here, not just in the CLI/report output, so it isn't lost
        # once the record leaves this run — queryable via `gcloud storage`
        # without needing to re-run validation or dig up the batch report.
        blob.metadata["needs_review"] = ",".join(needs_review)
    blob.patch()

    metadata_key = str(Path(key).with_name(Path(key).stem + ".metadata.json"))
    bucket.blob(metadata_key).upload_from_string(
        json.dumps(record, indent=2), content_type="application/json"
    )

    for extra in extra_files or []:
        extra_key = f"{Path(key).parent}/{Path(key).stem}/{extra.name}" if Path(key).parent != Path(".") else extra.name
        _gcloud_storage_cp(extra, f"gs://{bucket.name}/{extra_key}")

    return UploadResult(object_uri=dest_uri, skipped=False, crc32c=local_crc32c)
