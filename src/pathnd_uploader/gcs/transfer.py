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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from google.cloud import storage

from ..integrity import IntegrityIssue, IntegrityReport
from ..retry import is_transient, retry_transient
from .audit import _audit_object_with_retry, _iter_slide_blobs

# Mixed workload — the validation half is latency-bound like audit's, but rewrite() does
# real server-side work per object, so this sits between batch's upload concurrency and
# audit's — moderate default rather than audit's more aggressive one.
DEFAULT_TRANSFER_WORKERS = 10


@dataclass
class TransferResult:
    source_uri: str
    dest_uri: str
    copied: bool  # True if this run performed the copy
    integrity_report: IntegrityReport
    already_present: bool = False  # dest already held a byte-identical copy — an idempotent no-op success

    @property
    def is_inconclusive(self) -> bool:
        return self.integrity_report.is_inconclusive

    @property
    def in_destination(self) -> bool:
        """True if the object is now in the destination — whether this run
        copied it or it was already there. Both are successes.
        """
        return self.copied or self.already_present


@dataclass
class TransferSummary:
    results: list[TransferResult]

    @property
    def copied(self) -> list[TransferResult]:
        return [r for r in self.results if r.copied]

    @property
    def already_present(self) -> list[TransferResult]:
        return [r for r in self.results if r.already_present]

    @property
    def skipped(self) -> list[TransferResult]:
        # skipped = a real problem stopped the copy (validation failed), not
        # an idempotent no-op and not a transient/inconclusive error
        return [r for r in self.results if not r.in_destination and not r.is_inconclusive]

    @property
    def inconclusive(self) -> list[TransferResult]:
        return [r for r in self.results if r.is_inconclusive]


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

    Idempotent: if the destination already holds a byte-identical object
    (same crc32c — which a rewrite preserves), it's left untouched and no
    re-copy happens. This is what makes a large interrupted transfer safe to
    re-run without re-paying cross-region egress on everything already done.
    The existence check comes first, before validation, so a resumed run
    doesn't even re-read already-copied source objects.
    """
    key = dest_key or source_blob.name
    dest_uri = f"gs://{dest_bucket.name}/{key}"
    source_uri = f"gs://{source_blob.bucket.name}/{source_blob.name}"

    existing = dest_bucket.get_blob(key)
    if existing is not None and existing.crc32c is not None and existing.crc32c == source_blob.crc32c:
        report = IntegrityReport(location=source_uri, size_bytes=source_blob.size, checks_run=[], issues=[])
        return TransferResult(
            source_uri=source_uri, dest_uri=dest_uri, copied=False, already_present=True, integrity_report=report
        )

    report = _audit_object_with_retry(source_blob, deep=deep)
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


_transfer_object_with_retry = retry_transient(transfer_object)


def _transfer_object_safe(source_blob: storage.Blob, dest_bucket: storage.Bucket, *, dest_key: str, deep: bool) -> TransferResult:
    try:
        return _transfer_object_with_retry(source_blob, dest_bucket, dest_key=dest_key, deep=deep)
    except Exception as exc:  # noqa: BLE001 - one bad object shouldn't kill the whole bucket transfer
        source_uri = f"gs://{source_blob.bucket.name}/{source_blob.name}"
        dest_uri = f"gs://{dest_bucket.name}/{dest_key}"
        if is_transient(exc):
            # Not a finding about the file — we couldn't finish transferring it after
            # retries. Reporting this as "corrupted" would be actively misleading.
            issue = IntegrityIssue(
                check="transfer_incomplete",
                severity="inconclusive",
                message=f"could not complete the transfer after retries due to a network error: {exc} — re-run to get a verdict",
            )
        else:
            issue = IntegrityIssue(check="transfer_error", severity="error", message=f"{type(exc).__name__}: {exc}")
        return TransferResult(
            source_uri=source_uri,
            dest_uri=dest_uri,
            copied=False,
            integrity_report=IntegrityReport(location=source_uri, size_bytes=None, checks_run=[], issues=[issue]),
        )


def transfer_bucket(
    source_bucket_name: str,
    dest_bucket_name: str,
    *,
    prefix: str = "",
    dest_prefix: str | None = None,
    deep: bool = False,
    client: storage.Client | None = None,
    workers: int = DEFAULT_TRANSFER_WORKERS,
    on_start: Callable[[int], None] | None = None,
    on_result: Callable[[TransferResult], None] | None = None,
) -> TransferSummary:
    """`on_start`/`on_result` — see `audit_bucket`'s docstring; same purpose:
    a caller can show live progress or write results out incrementally
    instead of only learning anything once the whole transfer finishes.
    """
    client = client or storage.Client()
    source_bucket = client.bucket(source_bucket_name)
    dest_bucket = client.bucket(dest_bucket_name)

    blobs = list(_iter_slide_blobs(source_bucket, prefix))
    if on_start is not None:
        on_start(len(blobs))

    results: list[TransferResult] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _transfer_object_safe,
                blob,
                dest_bucket,
                dest_key=_remap_key(blob.name, prefix=prefix, dest_prefix=dest_prefix),
                deep=deep,
            )
            for blob in blobs
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if on_result is not None:
                on_result(result)

    return TransferSummary(results=results)
