"""Data model for a single parsed CDE (Common Data Element) field."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Field:
    """One row of the PathND-Core-Schema.csv, parsed into typed attributes."""

    name: str
    display_name: str
    type: str  # "string" | "float" | "int" | "enum" | "date" | "binary"
    description: str
    allowed_values: tuple[str, ...] | None
    nullable: bool
    constraints: dict | None  # e.g. {"min": 0.0, "max": 100.0}
    collection: str
    required: bool
    aliases: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Schema:
    """The full set of CDE fields plus the version they came from."""

    version: str
    source_commit: str
    fields_by_name: dict[str, Field]

    def __iter__(self):
        return iter(self.fields_by_name.values())

    def __len__(self) -> int:
        return len(self.fields_by_name)

    def get(self, name: str) -> Field | None:
        return self.fields_by_name.get(name)

    @property
    def required_field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self if f.required)
