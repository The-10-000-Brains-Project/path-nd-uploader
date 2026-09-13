"""Cross-checks between declared metadata and the actual slide file.

Two checks matter here for different reasons:

* `slide_paths` referring to a file that doesn't exist at all is a hard
  error — this is the specific gap called out as missing from the existing
  metadata-only validation tooling (it validates field values, not that the
  files those values point to exist).
* A declared scanner objective magnification disagreeing with what's
  embedded in the slide itself is only a warning: declared metadata can
  legitimately override/correct what a scanner wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .metadata import FieldError


@dataclass
class ReconciliationResult:
    errors: list[FieldError]
    warnings: list[FieldError]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def check_slide_path_exists(record: dict, resolved_path: Path) -> FieldError | None:
    if not resolved_path.exists():
        return FieldError(
            "slide_paths",
            f"metadata declares slide_paths={record.get('slide_paths')!r} but no file exists at {resolved_path}",
        )
    return None


def check_uploaded_file_matches_metadata(record: dict, uploaded_path: Path) -> FieldError | None:
    """Guards against a batch-run pairing bug: the metadata record being
    validated is not actually the one for the file about to be uploaded.
    """
    declared = record.get("slide_paths")
    if declared and Path(declared).name != uploaded_path.name:
        return FieldError(
            "slide_paths",
            f"metadata declares slide_paths={declared!r} but the file being uploaded is {uploaded_path.name!r}",
        )
    return None


def reconcile(record: dict, *, uploaded_path: Path, tech_metadata: dict) -> ReconciliationResult:
    errors: list[FieldError] = []
    warnings: list[FieldError] = []

    path_error = check_uploaded_file_matches_metadata(record, uploaded_path)
    if path_error:
        errors.append(path_error)

    declared_mag = record.get("scanner_objective_magnification")
    embedded_power = tech_metadata.get("objective_power")
    if declared_mag and embedded_power:
        try:
            if float(declared_mag) != float(embedded_power):
                warnings.append(
                    FieldError(
                        "scanner_objective_magnification",
                        f"declared magnification {declared_mag!r} does not match the slide's "
                        f"embedded objective power {embedded_power!r}",
                    )
                )
        except ValueError:
            pass  # e.g. declared "other" — not comparable, not an error

    return ReconciliationResult(errors=errors, warnings=warnings)
