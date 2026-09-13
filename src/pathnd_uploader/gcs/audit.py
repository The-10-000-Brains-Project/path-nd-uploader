"""Retroactively scans an already-populated bucket/prefix for integrity
issues, without downloading whole slides.

This exists because brain banks have been uploading via raw `gcloud`/
`gsutil` for a while, uploads have been observed to interrupt and restart,
and nothing has been checking file integrity after the fact — this replaces
that manual, ad hoc bucket sampling with a systematic pass.

Default is a "fast" scan (header magic, zero-tail, and a TIFF-directory
structural check — see `integrity.checks`) — a few small range-reads per
object, cheap enough to run across an entire bucket, and scanned with a
thread pool since the work is latency-bound, not bandwidth-bound. `deep=True`
opts into downloading each object that passes the fast scan and re-running
the full OpenSlide structural walk — expensive, off by default.
"""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from google.cloud import storage

from ..integrity import GCSBlobSource, IntegrityIssue, IntegrityReport, run_deep_structural_check, run_fast_checks
from ..retry import is_transient, retry_transient
from ..slide.formats import SUPPORTED_EXTENSIONS

# Scanning is latency-bound (many small range-reads per object), not bandwidth-bound like
# uploads, so a much higher default concurrency than batch.py's uploads is safe and valuable —
# a 5,000-object bucket at ~9s/object sequentially is hours; parallelized, it's minutes.
DEFAULT_AUDIT_WORKERS = 20


def _iter_slide_blobs(bucket: storage.Bucket, prefix: str) -> Iterator[storage.Blob]:
    for blob in bucket.list_blobs(prefix=prefix):
        if Path(blob.name).suffix.lower() in SUPPORTED_EXTENSIONS:
            yield blob


def audit_object(blob: storage.Blob, *, deep: bool = False) -> IntegrityReport:
    source = GCSBlobSource(blob)
    extension = Path(blob.name).suffix
    issues, tech_metadata = run_fast_checks(source, extension=extension)
    checks_run = ["header_magic", "zero_tail", "structural_completeness"]

    if deep and not any(i.severity == "error" for i in issues):
        with tempfile.NamedTemporaryFile(suffix=extension) as tmp:
            blob.download_to_filename(tmp.name)
            deep_issues, deep_metadata = run_deep_structural_check(Path(tmp.name))
            issues += deep_issues
            tech_metadata = {**tech_metadata, **deep_metadata}
            checks_run.append("deep_structural")

    return IntegrityReport(
        location=source.describe(),
        size_bytes=source.size(),
        checks_run=checks_run,
        issues=issues,
        tech_metadata=tech_metadata,
    )


@dataclass
class AuditSummary:
    total_scanned: int
    reports: list[IntegrityReport]

    @property
    def failed(self) -> list[IntegrityReport]:
        return [r for r in self.reports if not r.passed and not r.is_inconclusive]

    @property
    def inconclusive(self) -> list[IntegrityReport]:
        return [r for r in self.reports if r.is_inconclusive]


_audit_object_with_retry = retry_transient(audit_object)


def _audit_object_safe(blob: storage.Blob, *, deep: bool) -> IntegrityReport:
    try:
        return _audit_object_with_retry(blob, deep=deep)
    except Exception as exc:  # noqa: BLE001 - one bad object shouldn't kill the whole bucket scan
        location = f"gs://{blob.bucket.name}/{blob.name}"
        if is_transient(exc):
            # Not a finding about the file — we couldn't finish checking it after
            # retries. Reporting this as "corrupted" would be actively misleading.
            issue = IntegrityIssue(
                check="scan_incomplete",
                severity="inconclusive",
                message=f"could not complete the scan after retries due to a network error: {exc} — re-run to get a verdict",
            )
        else:
            issue = IntegrityIssue(check="scan_error", severity="error", message=f"{type(exc).__name__}: {exc}")
        return IntegrityReport(location=location, size_bytes=blob.size, checks_run=[], issues=[issue])


def audit_bucket(
    bucket_name: str,
    *,
    prefix: str = "",
    deep: bool = False,
    client: storage.Client | None = None,
    workers: int = DEFAULT_AUDIT_WORKERS,
    on_start: Callable[[int], None] | None = None,
    on_result: Callable[[IntegrityReport], None] | None = None,
) -> AuditSummary:
    """`on_start`, if given, is called once with the total object count as
    soon as it's known (listing the bucket happens inside this call, so a
    caller can't know the total up front otherwise). `on_result` is called
    with each object's report as soon as it's ready — see `run_batch`'s
    docstring for why that matters for long scans.
    """
    client = client or storage.Client()
    bucket = client.bucket(bucket_name)

    blobs = list(_iter_slide_blobs(bucket, prefix))
    if on_start is not None:
        on_start(len(blobs))

    reports: list[IntegrityReport] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_audit_object_safe, blob, deep=deep) for blob in blobs]
        for future in as_completed(futures):
            result = future.result()
            reports.append(result)
            if on_result is not None:
                on_result(result)

    return AuditSummary(total_scanned=len(reports), reports=reports)


def audit_manifest_against_bucket(
    records: list[dict],
    *,
    bucket_name: str,
    client: storage.Client | None = None,
) -> list[str]:
    """Cross-references a metadata manifest's declared `slide_paths` against
    what actually exists in the bucket. Returns the list of declared paths
    with no corresponding object — the "does the referenced file even
    exist" gap identified as missing from existing tooling.
    """
    client = client or storage.Client()
    bucket = client.bucket(bucket_name)

    missing = []
    for record in records:
        declared = record.get("slide_paths")
        if not declared:
            continue
        if bucket.get_blob(declared) is None:
            missing.append(declared)
    return missing
