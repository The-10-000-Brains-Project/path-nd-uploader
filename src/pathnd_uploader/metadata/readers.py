"""Loads metadata sidecars: one JSON object per slide, or a batch manifest."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def read_sidecar(path: Path) -> dict:
    """Reads a single slide's metadata sidecar (JSON)."""
    with Path(path).open(encoding="utf-8") as f:
        return json.load(f)


def read_manifest(path: Path) -> list[dict]:
    """Reads a batch manifest: CSV or JSON-Lines, one row/line per slide.

    Each record must include enough to locate the slide file itself — by
    convention the schema's `slide_paths` field, interpreted relative to the
    manifest's own directory unless it is absolute.
    """
    path = Path(path)
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        with path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    if path.suffix.lower() == ".json":
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{path} must contain a JSON array of per-slide records")
        return data
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    raise ValueError(f"Unsupported manifest format: {path.suffix} (use .csv, .json, or .jsonl)")


def resolve_slide_path(record: dict, *, manifest_dir: Path) -> Path:
    """Resolves a manifest record's declared `slide_paths` to an actual path
    on disk, relative to the manifest's own directory if not absolute.
    """
    declared = record.get("slide_paths")
    if not declared:
        raise KeyError("record has no 'slide_paths' field")
    declared_path = Path(declared)
    return declared_path if declared_path.is_absolute() else manifest_dir / declared_path
