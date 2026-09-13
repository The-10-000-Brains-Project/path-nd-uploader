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
    severity: str  # "error" | "warning" | "inconclusive"
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
        return not self.errors and not self.inconclusive_issues

    @property
    def is_inconclusive(self) -> bool:
        """True when nothing was found to be actually wrong with the file,
        but a network/transient error (after retries) meant it couldn't be
        fully checked — distinct from `passed=False`, which means a real
        finding about the file's content. Never both true at once: a real
        error takes precedence in how a caller should react.
        """
        return not self.errors and bool(self.inconclusive_issues)

    @property
    def errors(self) -> list[IntegrityIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[IntegrityIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def inconclusive_issues(self) -> list[IntegrityIssue]:
        return [i for i in self.issues if i.severity == "inconclusive"]
