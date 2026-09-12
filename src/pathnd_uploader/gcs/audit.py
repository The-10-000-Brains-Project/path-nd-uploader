"""Retroactively scans an already-populated bucket/prefix for integrity
issues, without downloading whole slides.

This exists because brain banks have been uploading via raw `gcloud`/
`gsutil` for a while, uploads have been observed to interrupt and restart,
and nothing has been checking file integrity after the fact — this replaces
that manual, ad hoc bucket sampling with a systematic pass.

Default is a "fast" scan (header magic + zero-tail, a few small range-reads
per object) cheap enough to run across an entire bucket. `deep=True` opts
into downloading each object that passes the fast scan and re-running the
full OpenSlide structural walk — expensive, off by default.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from google.cloud import storage

from ..integrity import GCSBlobSource, IntegrityReport, run_deep_structural_check, run_fast_checks
from ..slide.formats import SUPPORTED_EXTENSIONS


def _iter_slide_blobs(bucket: storage.Bucket, prefix: str) -> Iterator[storage.Blob]:
    for blob in bucket.list_blobs(prefix=prefix):
        if Path(blob.name).suffix.lower() in SUPPORTED_EXTENSIONS:
            yield blob


def audit_object(blob: storage.Blob, *, deep: bool = False) -> IntegrityReport:
    source = GCSBlobSource(blob)
    extension = Path(blob.name).suffix
    issues = run_fast_checks(source, extension=extension)
    checks_run = ["header_magic", "zero_tail"]
    tech_metadata: dict = {}

    if deep and not any(i.severity == "error" for i in issues):
        with tempfile.NamedTemporaryFile(suffix=extension) as tmp:
            blob.download_to_filename(tmp.name)
            deep_issues, tech_metadata = run_deep_structural_check(Path(tmp.name))
            issues += deep_issues
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
        return [r for r in self.reports if not r.passed]


def audit_bucket(
    bucket_name: str,
    *,
    prefix: str = "",
    deep: bool = False,
    client: storage.Client | None = None,
) -> AuditSummary:
    client = client or storage.Client()
    bucket = client.bucket(bucket_name)

    reports = [audit_object(blob, deep=deep) for blob in _iter_slide_blobs(bucket, prefix)]
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
