"""Copies validated slides from one GCS bucket to another.

Not the primary workflow — most uploads go through `upload`/`batch` from a
local file. This exists for moving already-uploaded slides between buckets
(e.g. consolidating buckets, cross-region replication) without round-
tripping the data through the local machine.

Uses GCS's server-side `rewrite` — Google copies the bytes directly between
buckets; they never pass through this process. That also means a copy can't
introduce new transfer corruption the way a local re-upload could, but it
will just as faithfully copy an already-corrupted source object, which is
exactly why the source is validated first (same fast-scan integrity core
`audit.py` uses) rather than treating "the copy succeeded" as sufficient.
Requires the caller's IAM identity to have read on the source bucket and
write on the destination — ordinary GCS permissions, nothing bespoke.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from google.cloud import storage

from ..integrity import IntegrityReport
from .audit import _iter_slide_blobs, audit_object


@dataclass
class TransferResult:
    source_uri: str
    dest_uri: str
    copied: bool  # False if validation failed, so nothing was transferred
    integrity_report: IntegrityReport


@dataclass
class TransferSummary:
    results: list[TransferResult]

    @property
    def copied(self) -> list[TransferResult]:
        return [r for r in self.results if r.copied]

    @property
    def skipped(self) -> list[TransferResult]:
        return [r for r in self.results if not r.copied]


def _remap_key(key: str, *, prefix: str, dest_prefix: str | None) -> str:
    if dest_prefix is None:
        return key
    if prefix and key.startswith(prefix):
        return dest_prefix.rstrip("/") + "/" + key[len(prefix) :].lstrip("/")
    return dest_prefix.rstrip("/") + "/" + key


def transfer_object(
    source_blob: storage.Blob,
    dest_bucket: storage.Bucket,
    *,
    dest_key: str | None = None,
    deep: bool = False,
) -> TransferResult:
    """Validates `source_blob` (fast scan by default, `deep=True` downloads
    it for the full structural check), then server-side copies it to
    `dest_bucket` only if that passes. Skips the copy entirely otherwise.
    """
    key = dest_key or source_blob.name
    dest_uri = f"gs://{dest_bucket.name}/{key}"
    source_uri = f"gs://{source_blob.bucket.name}/{source_blob.name}"

    report = audit_object(source_blob, deep=deep)
    if not report.passed:
        return TransferResult(source_uri=source_uri, dest_uri=dest_uri, copied=False, integrity_report=report)

    dest_blob = dest_bucket.blob(key)
    token = None
    while True:
        token, _bytes_written, _total_bytes = dest_blob.rewrite(source_blob, token=token)
        if token is None:
            break

    dest_blob.metadata = {
        **(dest_blob.metadata or {}),
        "transferred_from": source_uri,
        "transferred_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    dest_blob.patch()

    return TransferResult(source_uri=source_uri, dest_uri=dest_uri, copied=True, integrity_report=report)


def transfer_bucket(
    source_bucket_name: str,
    dest_bucket_name: str,
    *,
    prefix: str = "",
    dest_prefix: str | None = None,
    deep: bool = False,
    client: storage.Client | None = None,
) -> TransferSummary:
    client = client or storage.Client()
    source_bucket = client.bucket(source_bucket_name)
    dest_bucket = client.bucket(dest_bucket_name)

    results = [
        transfer_object(
            blob,
            dest_bucket,
            dest_key=_remap_key(blob.name, prefix=prefix, dest_prefix=dest_prefix),
            deep=deep,
        )
        for blob in _iter_slide_blobs(source_bucket, prefix)
    ]
    return TransferSummary(results=results)
