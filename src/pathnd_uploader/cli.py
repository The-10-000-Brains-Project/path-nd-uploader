from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .batch import (
    DEFAULT_STABILITY_WAIT_SECONDS,
    BatchItem,
    discover_pairs_in_directory,
    items_from_manifest,
    process_item,
    run_batch,
)
from .config import get_bucket
from .gcs.audit import audit_bucket, audit_manifest_against_bucket
from .gcs.transfer import transfer_bucket
from .mapping import PROFILES
from .metadata import read_manifest
from .report import write_audit_report, write_batch_report, write_transfer_report
from .schema import load_schema

app = typer.Typer(help="Validate and upload pathology whole-slide images + CDE metadata to GCS.")


def _print_item_result(result) -> None:
    status = "PASS" if result.passed else "FAIL"
    typer.echo(f"[{status}] {result.slide_path}")
    if result.error:
        typer.echo(f"    error: {result.error}")
    if result.integrity_report:
        for issue in result.integrity_report.issues:
            typer.echo(f"    [{issue.severity}] {issue.check}: {issue.message}")
    if result.metadata_result:
        for e in result.metadata_result.errors:
            typer.echo(f"    [error] metadata.{e.field}: {e.message}")
        for r in result.metadata_result.needs_review:
            typer.echo(f"    [NEEDS REVIEW] metadata.{r.field}: {r.message}")
        for w in result.metadata_result.warnings:
            typer.echo(f"    [warning] metadata.{w.field}: {w.message}")
    if result.reconciliation:
        for e in result.reconciliation.errors:
            typer.echo(f"    [error] reconcile.{e.field}: {e.message}")
        for w in result.reconciliation.warnings:
            typer.echo(f"    [warning] reconcile.{e.field}: {w.message}")
    if result.upload:
        note = " (skipped, already present)" if result.upload.skipped else ""
        typer.echo(f"    uploaded -> {result.upload.object_uri}{note}")


@app.command()
def validate(
    slide_path: Path,
    metadata: Optional[Path] = typer.Option(None, "--metadata", help="Path to the slide's JSON metadata sidecar"),
    deep: bool = typer.Option(True, help="Run the full OpenSlide structural check (in addition to the fast checks)"),
    strict: bool = typer.Option(False, help="Reject metadata fields not present in the CDE schema"),
    stability_wait: float = typer.Option(
        DEFAULT_STABILITY_WAIT_SECONDS,
        "--stability-wait",
        help="Seconds to confirm the file isn't still being written before validating it (0 to skip)",
    ),
):
    """Dry-run validation only — no GCS calls. Exits non-zero on failure."""
    result = process_item(slide_path, metadata, deep=deep, strict=strict, stability_wait_seconds=stability_wait)
    _print_item_result(result)
    raise typer.Exit(code=0 if result.passed else 1)


@app.command()
def upload(
    slide_path: Path,
    bucket: str = typer.Option(..., "--bucket"),
    metadata: Path = typer.Option(..., "--metadata", help="Path to the slide's JSON metadata sidecar"),
    deep: bool = typer.Option(True, help="Run the full OpenSlide structural check before uploading"),
    strict: bool = typer.Option(False, help="Reject metadata fields not present in the CDE schema"),
    stability_wait: float = typer.Option(
        DEFAULT_STABILITY_WAIT_SECONDS,
        "--stability-wait",
        help="Seconds to confirm the file isn't still being written before validating it (0 to skip)",
    ),
):
    """Validates one slide + its metadata, then uploads only if validation passes."""
    gcs_bucket = get_bucket(bucket)
    result = process_item(
        slide_path, metadata, bucket=gcs_bucket, deep=deep, strict=strict, stability_wait_seconds=stability_wait
    )
    _print_item_result(result)
    raise typer.Exit(code=0 if result.passed else 1)


@app.command(name="batch")
def batch_cmd(
    source: Path = typer.Argument(
        ..., help="A directory of slide+.json pairs, or a manifest (.csv/.json/.jsonl/.xlsx)"
    ),
    bucket: Optional[str] = typer.Option(None, "--bucket", help="Upload on success; omit to only validate"),
    workers: int = typer.Option(6, help="Parallel worker count"),
    deep: bool = typer.Option(True, help="Run the full OpenSlide structural check on each slide"),
    strict: bool = typer.Option(False, help="Reject metadata fields not present in the CDE schema"),
    report: Optional[Path] = typer.Option(None, "--report", help="Write a JSON run report to this path"),
    profile: Optional[str] = typer.Option(
        None, "--profile", help=f"Map a raw institutional manifest to CDE fields first. Available: {sorted(PROFILES)}"
    ),
    sheet: Optional[str] = typer.Option(
        None, "--sheet", help="Worksheet name to read, for .xlsx manifests with multiple sheets (default: the first)"
    ),
    stability_wait: float = typer.Option(
        DEFAULT_STABILITY_WAIT_SECONDS,
        "--stability-wait",
        help="Seconds to confirm each file isn't still being written before validating it (0 to skip)",
    ),
):
    """Validates (and optionally uploads) many slides at once."""
    if source.is_dir():
        items: list[BatchItem] = discover_pairs_in_directory(source)
    else:
        source_profile = None
        if profile is not None:
            if profile not in PROFILES:
                typer.echo(f"Unknown profile {profile!r}. Available: {sorted(PROFILES)}")
                raise typer.Exit(code=2)
            source_profile = PROFILES[profile]
        records = read_manifest(source, sheet=sheet)
        items = items_from_manifest(records, manifest_dir=source.parent, profile=source_profile)

    gcs_bucket = get_bucket(bucket) if bucket else None
    results = run_batch(
        items, bucket=gcs_bucket, deep=deep, strict=strict, workers=workers, stability_wait_seconds=stability_wait
    )
    for r in results:
        _print_item_result(r)
    if report:
        write_batch_report(results, report)
    failed = sum(1 for r in results if not r.passed)
    typer.echo(f"\n{len(results) - failed}/{len(results)} passed")
    raise typer.Exit(code=0 if failed == 0 else 1)


@app.command()
def audit(
    bucket: str,
    prefix: str = typer.Option("", help="Only scan objects under this prefix"),
    deep: bool = typer.Option(False, help="Also download+re-open objects that pass the fast scan (expensive)"),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Cross-reference a metadata manifest's slide_paths against bucket contents"
    ),
    report: Optional[Path] = typer.Option(None, "--report", help="Write a JSON audit report to this path"),
):
    """Scans an already-populated bucket/prefix for corrupted (e.g.
    truncated/zero-filled) slide files, without downloading them by default.
    """
    summary = audit_bucket(bucket, prefix=prefix, deep=deep)
    for r in summary.reports:
        status = "PASS" if r.passed else "FAIL"
        typer.echo(f"[{status}] {r.location} ({r.size_bytes:,} bytes)")
        for issue in r.issues:
            typer.echo(f"    [{issue.severity}] {issue.check}: {issue.message}")

    if manifest:
        missing = audit_manifest_against_bucket(read_manifest(manifest), bucket_name=bucket)
        if missing:
            typer.echo(f"\n{len(missing)} manifest slide_paths have no corresponding object in the bucket:")
            for m in missing:
                typer.echo(f"    {m}")

    if report:
        write_audit_report(summary.reports, report)

    typer.echo(f"\n{summary.total_scanned - len(summary.failed)}/{summary.total_scanned} passed")
    raise typer.Exit(code=0 if not summary.failed else 1)


@app.command()
def transfer(
    source_bucket: str,
    dest_bucket: str,
    prefix: str = typer.Option("", help="Only transfer objects under this prefix in the source bucket"),
    dest_prefix: Optional[str] = typer.Option(
        None, "--dest-prefix", help="Replace `--prefix` with this in the destination key (default: same key)"
    ),
    deep: bool = typer.Option(False, help="Download+fully validate each object before copying (expensive)"),
    report: Optional[Path] = typer.Option(None, "--report", help="Write a JSON transfer report to this path"),
):
    """Copies validated slides from one GCS bucket to another (server-side —
    data moves directly between buckets, not through this machine). Requires
    your GCS identity to have read on the source and write on the
    destination. Not the primary workflow; most uploads come from local
    files via `upload`/`batch`.
    """
    summary = transfer_bucket(source_bucket, dest_bucket, prefix=prefix, dest_prefix=dest_prefix, deep=deep)
    for r in summary.results:
        status = "COPIED" if r.copied else "SKIPPED"
        typer.echo(f"[{status}] {r.source_uri} -> {r.dest_uri}")
        for issue in r.integrity_report.issues:
            typer.echo(f"    [{issue.severity}] {issue.check}: {issue.message}")

    if report:
        write_transfer_report(summary.results, report)

    typer.echo(f"\n{len(summary.copied)}/{len(summary.results)} copied")
    raise typer.Exit(code=0 if not summary.skipped else 1)


schema_app = typer.Typer(help="Inspect the pinned CDE schema.")
app.add_typer(schema_app, name="schema")


@schema_app.command("show")
def schema_show():
    schema = load_schema()
    typer.echo(f"CDE schema version: {schema.version} (source commit {schema.source_commit})")
    typer.echo(f"{len(schema)} fields, {len(schema.required_field_names)} required:")
    for name in schema.required_field_names:
        typer.echo(f"    {name}")


@schema_app.command("update")
def schema_update(ref: str = typer.Argument(..., help="Git ref (tag or commit) in the pathnd-cdes repo to pin to")):
    typer.echo("Run: scripts/update_schema.sh " + ref)
    typer.echo("(kept as an explicit script rather than a CLI side-effect, so schema pins are a reviewable diff)")


if __name__ == "__main__":
    app()
