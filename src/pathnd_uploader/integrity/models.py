"""Result types for the shared file-integrity check core.

These are deliberately independent of *where* the bytes came from (local
disk pre-upload, or an existing GCS object during an audit) so the same
report shape works for both entry points.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IntegrityIssue:
    check: str  # e.g. "zero_tail", "header_magic", "structural_open", "level_read", "path_missing"
    severity: str  # "error" | "warning"
    message: str


@dataclass
class IntegrityReport:
    location: str  # local path or gs:// URI
    size_bytes: int | None
    checks_run: list[str] = field(default_factory=list)
    issues: list[IntegrityIssue] = field(default_factory=list)
    tech_metadata: dict = field(default_factory=dict)  # vendor, objective_power, mpp_x/y, level_count, dimensions

    @property
    def passed(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> list[IntegrityIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[IntegrityIssue]:
        return [i for i in self.issues if i.severity == "warning"]
