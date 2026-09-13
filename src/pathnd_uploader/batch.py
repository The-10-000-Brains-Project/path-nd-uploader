"""Orchestrates the full validate(+upload) pipeline for one or many slides.

Single-item and batch runs share this same per-item pipeline; batch mode
just fans it out across a thread pool (the work is I/O-bound: hashing large
files and uploading them) and aggregates results into one report.

Metadata always comes from a manifest row (a dict) — there's no supported
per-slide sidecar-file format. Every real institutional export examined
(BDR's CSV, Mount Sinai/PART's xlsx) is one spreadsheet covering many
slides, not a file per slide; a standalone `slide.json` next to `slide.svs`
was an earlier design guess with no real-world basis, and has been removed.
For a single slide, pass a manifest with one row.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from google.cloud import storage

from .gcs.uploader import UploadResult, upload_slide
from .integrity import IntegrityReport
from .mapping import SourceProfile, apply_profile
from .metadata import ValidationResult, resolve_slide_path, validate_metadata
from .reconcile import ReconciliationResult, check_slide_path_exists, reconcile
from .schema import Schema, load_schema
from .slide import validate_slide
from .slide.formats import SUPPORTED_EXTENSIONS
from .slide.validator import DEFAULT_STABILITY_WAIT_SECONDS

DEFAULT_WORKERS = 6


@dataclass
class BatchItem:
    """One unit of work: a slide, and optionally its metadata record."""

    slide_path: Path | None  # None when the manifest row has no resolvable slide_paths at all
    record: dict | None = None  # a manifest row; None means integrity-only, no metadata to check
    known_gaps: frozenset[str] = frozenset()  # from the source profile, if any — see SourceProfile.known_gaps


@dataclass
class ItemResult:
    slide_path: str
    metadata_result: ValidationResult | None = None
    integrity_report: IntegrityReport | None = None
    reconciliation: ReconciliationResult | None = None
    upload: UploadResult | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        if self.metadata_result is not None and not self.metadata_result.is_valid:
            return False
        if self.integrity_report is not None and not self.integrity_report.passed:
            return False
        if self.reconciliation is not None and not self.reconciliation.is_valid:
            return False
        return True


def process_batch_item(
    item: BatchItem,
    *,
    schema: Schema | None = None,
    bucket: storage.Bucket | None = None,
    deep: bool = True,
    strict: bool = False,
    stability_wait_seconds: float = DEFAULT_STABILITY_WAIT_SECONDS,
) -> ItemResult:
    schema = schema or load_schema()

    if item.slide_path is None:
        declared = (item.record or {}).get("slide_paths")
        return ItemResult(
            slide_path=f"<unresolved: slide_paths={declared!r}>",
            error="no resolvable slide_paths in this record — nothing to validate or upload",
        )

    slide_path = Path(item.slide_path)
    result = ItemResult(slide_path=str(slide_path))

    try:
        record = item.record or {}
        has_metadata = item.record is not None
        if has_metadata:
            result.metadata_result = validate_metadata(
                record, schema=schema, strict=strict, downgrade_to_warning=item.known_gaps
            )

        result.integrity_report = validate_slide(slide_path, deep=deep, stability_wait_seconds=stability_wait_seconds)

        if has_metadata and record.get("slide_paths"):
            path_error = check_slide_path_exists(record, slide_path)
            recon = reconcile(record, uploaded_path=slide_path, tech_metadata=result.integrity_report.tech_metadata)
            if path_error:
                recon.errors.append(path_error)
            result.reconciliation = recon

        if bucket is not None and result.passed:
            needs_review = [e.field for e in result.metadata_result.needs_review] if result.metadata_result else None
            result.upload = upload_slide(
                bucket=bucket, slide_path=slide_path, record=record, schema=schema, needs_review=needs_review
            )
    except Exception as exc:  # noqa: BLE001 - surfaced in the report, not raised, so one bad item doesn't kill a batch
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def process_item(
    slide_path: Path,
    record: dict | None,
    *,
    schema: Schema | None = None,
    bucket: storage.Bucket | None = None,
    deep: bool = True,
    strict: bool = False,
    stability_wait_seconds: float = DEFAULT_STABILITY_WAIT_SECONDS,
) -> ItemResult:
    """Single-item convenience wrapper around `process_batch_item`. `record`
    is an already-loaded metadata dict (e.g. the one row of a manifest for
    this slide) — pass None to run an integrity-only check with no metadata.
    """
    return process_batch_item(
        BatchItem(slide_path=Path(slide_path), record=record),
        schema=schema,
        bucket=bucket,
        deep=deep,
        strict=strict,
        stability_wait_seconds=stability_wait_seconds,
    )


def discover_slides_in_directory(directory: Path) -> list[BatchItem]:
    """Finds supported slide files in a directory for an integrity-only scan
    — no metadata is inferred. Pass a manifest (with `--profile` if it's a
    raw institutional export) if you also want metadata validated.
    """
    directory = Path(directory)
    return [BatchItem(slide_path=p) for p in sorted(directory.iterdir()) if p.suffix.lower() in SUPPORTED_EXTENSIONS]


def items_from_manifest(
    records: list[dict], *, manifest_dir: Path, profile: SourceProfile | None = None
) -> list[BatchItem]:
    """Builds batch items from manifest rows, each carrying its metadata inline
    and resolving `slide_paths` relative to the manifest's own directory.

    Pass `profile` when the manifest is a raw institutional export (not
    already CDE-shaped) — see `pathnd_uploader.mapping`.
    """
    known_gaps = profile.known_gaps if profile is not None else frozenset()
    if profile is not None:
        records = [apply_profile(r, profile) for r in records]

    items = []
    for record in records:
        try:
            slide_path = resolve_slide_path(record, manifest_dir=manifest_dir)
        except KeyError:
            slide_path = None  # surfaced as a per-item failure in process_batch_item, not a crash here
        items.append(BatchItem(slide_path=slide_path, record=record, known_gaps=known_gaps))
    return items


def run_batch(
    items: list[BatchItem],
    *,
    bucket: storage.Bucket | None = None,
    deep: bool = True,
    strict: bool = False,
    workers: int = DEFAULT_WORKERS,
    stability_wait_seconds: float = DEFAULT_STABILITY_WAIT_SECONDS,
    on_result: Callable[[ItemResult], None] | None = None,
) -> list[ItemResult]:
    """`on_result`, if given, is called with each item's result as soon as
    it's ready (not batched to the end) — lets a caller show live progress
    or write results out incrementally, e.g. so a long run's output survives
    being interrupted partway instead of only being written at completion.
    """
    schema = load_schema()  # load once up front; process_batch_item's default would reparse the cache key each call
    results: list[ItemResult] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                process_batch_item,
                item,
                schema=schema,
                bucket=bucket,
                deep=deep,
                strict=strict,
                stability_wait_seconds=stability_wait_seconds,
            )
            for item in items
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if on_result is not None:
                on_result(result)
    return results
