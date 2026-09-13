"""Serializes run results (from `batch.py`, `gcs/audit.py`, `gcs/transfer.py`)
to JSON-lines, one object per line, written and flushed as each result
completes — not batched into a single write at the end. A long run that gets
interrupted (Ctrl-C, crash, laptop sleep) still leaves a usable, readable
file behind, and the file can be tailed live (`tail -f`) from another
terminal for progress feedback outside the main one.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .batch import ItemResult
from .gcs.transfer import TransferResult
from .integrity import IntegrityReport


class IncrementalReportWriter:
    def __init__(self, path: Path):
        self._file = open(path, "w", encoding="utf-8")

    def write(self, data: dict) -> None:
        self._file.write(json.dumps(data, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "IncrementalReportWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def item_result_to_dict(result: ItemResult) -> dict:
    d = {
        "slide_path": result.slide_path,
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
        d["integrity"] = integrity_report_to_dict(result.integrity_report)
    if result.reconciliation:
        d["reconciliation"] = {
            "errors": [asdict(e) for e in result.reconciliation.errors],
            "warnings": [asdict(e) for e in result.reconciliation.warnings],
        }
    if result.upload:
        d["upload"] = asdict(result.upload)
    return d


def integrity_report_to_dict(report: IntegrityReport) -> dict:
    return {
        "location": report.location,
        "size_bytes": report.size_bytes,
        "passed": report.passed,
        "checks_run": report.checks_run,
        "issues": [asdict(i) for i in report.issues],
        "tech_metadata": report.tech_metadata,
    }


def transfer_result_to_dict(result: TransferResult) -> dict:
    return {
        "source_uri": result.source_uri,
        "dest_uri": result.dest_uri,
        "copied": result.copied,
        # recorded so a saved report can distinguish an idempotent no-op
        # success (already there) from a real problem-skip — the live CLI
        # tally already separates these, but the JSON report was conflating them
        "already_present": result.already_present,
        "integrity": integrity_report_to_dict(result.integrity_report),
    }
