"""Validates a metadata record against the vendored PathND CDE schema."""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from ..schema import Field, Schema, load_schema


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str


@dataclass
class ValidationResult:
    schema_version: str
    errors: list[FieldError] = field(default_factory=list)
    warnings: list[FieldError] = field(default_factory=list)
    # Would normally be an error, but the source profile has declared this
    # field as a known gap/ambiguity for its export — doesn't block, but is
    # kept separate from routine "unknown field" warnings so it stays visible.
    needs_review: list[FieldError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _check_type(name: str, value, field_def: Field) -> FieldError | None:
    t = field_def.type
    if t == "string":
        if not isinstance(value, str):
            return FieldError(name, f"expected a string, got {type(value).__name__}")
    elif t in ("float", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            try:
                float(value)
            except (TypeError, ValueError):
                return FieldError(name, f"expected a {t}, got {value!r}")
    elif t == "boolean":
        if isinstance(value, str):
            if value.strip().lower() not in ("true", "false"):
                return FieldError(name, f"expected true/false, got {value!r}")
        elif not isinstance(value, bool):
            return FieldError(name, f"expected a boolean, got {value!r}")
    elif t == "enum":
        allowed = field_def.allowed_values or ()
        str_value = value if isinstance(value, str) else str(value)
        if str_value not in allowed:
            return FieldError(name, f"{value!r} is not one of the allowed values {list(allowed)}")
    elif t == "date":
        if not isinstance(value, str):
            return FieldError(name, f"expected an ISO date string, got {type(value).__name__}")
        try:
            _dt.date.fromisoformat(value)
        except ValueError:
            return FieldError(name, f"{value!r} is not a valid ISO date (YYYY-MM-DD)")
    return None


def _check_constraints(name: str, value, field_def: Field) -> FieldError | None:
    if not field_def.constraints:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None  # type check already reported this
    lo = field_def.constraints.get("min")
    hi = field_def.constraints.get("max")
    if lo is not None and numeric < lo:
        return FieldError(name, f"{value} is below the minimum allowed value {lo}")
    if hi is not None and numeric > hi:
        return FieldError(name, f"{value} is above the maximum allowed value {hi}")
    return None


def validate_metadata(
    record: dict,
    *,
    schema: Schema | None = None,
    strict: bool = False,
    downgrade_to_warning: frozenset[str] = frozenset(),
) -> ValidationResult:
    """Validates one metadata record (e.g. the contents of a slide's JSON
    sidecar) against the CDE schema.

    In non-strict mode (default), fields present in the record but not in
    the schema are reported as warnings (likely typos) rather than errors,
    since the schema evolves and callers may be validating against an older
    pinned version. Pass `strict=True` to reject unknown fields outright.

    `downgrade_to_warning` names fields where what would normally be an
    error should instead land in `result.needs_review` — for a known,
    source-level gap or ambiguity (see `SourceProfile.known_gaps`) that a
    human should follow up on, but that shouldn't block the batch.
    """
    schema = schema or load_schema()
    result = ValidationResult(schema_version=schema.version)

    def report(error: FieldError) -> None:
        target = result.needs_review if error.field in downgrade_to_warning else result.errors
        target.append(error)

    for field_def in schema:
        if field_def.name not in record:
            if field_def.required:
                report(FieldError(field_def.name, "required field is missing"))
            continue

        value = record[field_def.name]
        if value is None or (isinstance(value, str) and value.strip() == ""):
            if field_def.required:
                report(FieldError(field_def.name, "required field is empty"))
            elif not field_def.nullable:
                report(FieldError(field_def.name, "field is not nullable but was empty/null"))
            continue

        for error in (_check_type(field_def.name, value, field_def), _check_constraints(field_def.name, value, field_def)):
            if error is not None:
                report(error)

    known_names = set(schema.fields_by_name)
    for key in record:
        if key not in known_names:
            unknown = FieldError(key, "field is not defined in the CDE schema")
            (result.errors if strict else result.warnings).append(unknown)

    return result
