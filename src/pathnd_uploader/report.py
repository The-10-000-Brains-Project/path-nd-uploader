"""Serializes run results (from `batch.py` or `gcs/audit.py`) to JSON for
machine consumption (CI, downstream reporting) and a short human summary.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .batch import ItemResult
from .gcs.transfer import TransferResult
from .integrity import IntegrityReport


def item_result_to_dict(result: ItemResult) -> dict:
    d = {
        "slide_path": result.slide_path,
        "metadata_path": result.metadata_path,
        "passed": result.passed,
        "error": result.error,
    }
    if result.metadata_result:
        d["metadata"] = {
            "schema_version": result.metadata_result.schema_version,
            "errors": [asdict(e) for e in result.metadata_result.errors],
            "needs_review": [asdict(e) for e in result.metadata_result.needs_review],
            "warnings": [asdict(e) for e in result.metadata_result.warnings],
        }
    if result.integrity_report:
        d["integrity"] = _integrity_report_to_dict(result.integrity_report)
    if result.reconciliation:
        d["reconciliation"] = {
            "errors": [asdict(e) for e in result.reconciliation.errors],
            "warnings": [asdict(e) for e in result.reconciliation.warnings],
        }
    if result.upload:
        d["upload"] = asdict(result.upload)
    return d


def _integrity_report_to_dict(report: IntegrityReport) -> dict:
    return {
        "location": report.location,
        "size_bytes": report.size_bytes,
        "passed": report.passed,
        "checks_run": report.checks_run,
        "issues": [asdict(i) for i in report.issues],
        "tech_metadata": report.tech_metadata,
    }


def write_batch_report(results: list[ItemResult], path: Path) -> None:
    payload = {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "items": [item_result_to_dict(r) for r in results],
    }
    Path(path).write_text(json.dumps(payload, indent=2, default=str))


def write_audit_report(reports: list[IntegrityReport], path: Path) -> None:
    payload = {
        "total_scanned": len(reports),
        "passed": sum(1 for r in reports if r.passed),
        "failed": sum(1 for r in reports if not r.passed),
        "items": [_integrity_report_to_dict(r) for r in reports],
    }
    Path(path).write_text(json.dumps(payload, indent=2, default=str))


def write_transfer_report(results: list[TransferResult], path: Path) -> None:
    payload = {
        "total": len(results),
        "copied": sum(1 for r in results if r.copied),
        "skipped": sum(1 for r in results if not r.copied),
        "items": [
            {
                "source_uri": r.source_uri,
                "dest_uri": r.dest_uri,
                "copied": r.copied,
                "integrity": _integrity_report_to_dict(r.integrity_report),
            }
            for r in results
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2, default=str))
