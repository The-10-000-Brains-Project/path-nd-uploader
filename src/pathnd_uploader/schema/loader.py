"""Loads the vendored PathND CDE schema CSV into a Schema of Field objects.

The CSV is treated as a pinned, versioned artifact (see
``vendor/pathnd-cdes/VERSION`` and ``scripts/update_schema.sh``) rather than
something fetched live, so validation results are reproducible run to run.
"""

from __future__ import annotations

import ast
import csv
from functools import lru_cache
from pathlib import Path

from .models import Field, Schema

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "pathnd-cdes"
_SCHEMA_CSV = _VENDOR_DIR / "PathND-Core-Schema.csv"
_VERSION_FILE = _VENDOR_DIR / "VERSION"
_SOURCE_COMMIT_FILE = _VENDOR_DIR / "SOURCE_COMMIT.txt"


class SchemaLoadError(Exception):
    """Raised when the vendored schema CSV is missing or malformed."""


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() == "true"


def _parse_literal(raw: str, *, column: str, row_name: str):
    raw = raw.strip()
    if not raw:
        return None
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError) as exc:
        raise SchemaLoadError(
            f"Could not parse '{column}' for field '{row_name}': {raw!r}"
        ) from exc


def _row_to_field(row: dict) -> Field:
    name = row["name"].strip()
    allowed_values = _parse_literal(row["allowed_values"], column="allowed_values", row_name=name)
    constraints = _parse_literal(row["constraints"], column="constraints", row_name=name)
    aliases = _parse_literal(row["aliases"], column="aliases", row_name=name) or []

    return Field(
        name=name,
        display_name=row["display_name"].strip(),
        type=row["type"].strip().lower(),
        description=row["description"].strip(),
        allowed_values=tuple(allowed_values) if allowed_values else None,
        nullable=_parse_bool(row["nullable"]),
        constraints=constraints,
        collection=row["collection"].strip(),
        required=row["priority"].strip().lower() == "required",
        aliases=tuple(aliases),
    )


@lru_cache(maxsize=1)
def load_schema(csv_path: str | Path | None = None) -> Schema:
    """Parses the vendored (or explicitly given) CDE schema CSV.

    Cached, since batch runs validate many records against the same schema.
    """
    path = Path(csv_path) if csv_path else _SCHEMA_CSV
    if not path.exists():
        raise SchemaLoadError(
            f"Schema CSV not found at {path}. Run scripts/update_schema.sh to vendor one."
        )

    fields_by_name: dict[str, Field] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("name", "").strip():
                continue
            parsed = _row_to_field(row)
            fields_by_name[parsed.name] = parsed

    version = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "unknown"
    source_commit = (
        _SOURCE_COMMIT_FILE.read_text().strip() if _SOURCE_COMMIT_FILE.exists() else "unknown"
    )

    return Schema(version=version, source_commit=source_commit, fields_by_name=fields_by_name)
