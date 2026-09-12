"""Framework for mapping a brain bank's raw export into CDE-shaped records.

Real institutional exports don't arrive pre-shaped to the CDE schema (raw
column names, no `slide_paths`/`stain_type` fields, per-slide identity
embedded in a filename convention rather than a dedicated column, etc.). A
`SourceProfile` captures one institution's translation from its raw export
to the fields `validate_metadata`/`reconcile`/`upload_slide` expect,
without touching any of that downstream pipeline.

Unmapped raw columns are deliberately preserved (not dropped) under their
original names, so they still surface as "unknown field" warnings rather
than silently vanishing — useful both for catching mapping gaps and for not
losing data the schema doesn't yet cover.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class SourceProfile:
    name: str
    # raw column name -> CDE field name, for simple 1:1 renames
    column_renames: dict[str, str] = field(default_factory=dict)
    # CDE field name -> fixed value to inject on every record (e.g. a study name
    # that isn't itself a column in the raw export)
    constants: dict[str, object] = field(default_factory=dict)
    # Given the ORIGINAL raw record, returns extra/overriding CDE fields that need
    # real parsing logic (e.g. extracting stain_type from a filename convention).
    # Runs after renames/constants and wins on any conflict.
    derive: Callable[[dict], dict] | None = None
    # CDE field names where this source has a known, structural gap or
    # ambiguity (e.g. a required field with no populated source column at
    # all, or a value that needs a human's domain judgment) — validation
    # failures on these fields are reported as `needs_review`, not `errors`,
    # so a batch isn't blocked on a problem mapping can't actually fix.
    known_gaps: frozenset[str] = frozenset()


def apply_profile(raw_record: dict, profile: SourceProfile) -> dict:
    record = dict(raw_record)
    for old_name, new_name in profile.column_renames.items():
        if old_name in record:
            record[new_name] = record.pop(old_name)
    record.update(profile.constants)
    if profile.derive is not None:
        record.update(profile.derive(raw_record))
    return record
