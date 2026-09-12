"""Pre-upload validation of a local slide file.

Thin orchestration over the shared `integrity` core: run the fast checks
first (cheap, catch the common truncated-upload case immediately), and only
run the expensive deep OpenSlide walk if those pass — no point spending
seconds opening a pyramid we already know is truncated.
"""

from __future__ import annotations

from pathlib import Path

from ..integrity import IntegrityIssue, IntegrityReport, LocalFileSource, run_deep_structural_check, run_fast_checks
from .formats import UnsupportedSlideFormat, validate_extension


def validate_slide(path: Path, *, deep: bool = True) -> IntegrityReport:
    path = Path(path)
    checks_run = []

    if not path.exists():
        return IntegrityReport(
            location=str(path),
            size_bytes=None,
            checks_run=[],
            issues=[IntegrityIssue(check="path_exists", severity="error", message="file does not exist")],
        )

    try:
        validate_extension(path)
    except UnsupportedSlideFormat as exc:
        return IntegrityReport(
            location=str(path),
            size_bytes=path.stat().st_size,
            issues=[IntegrityIssue(check="extension", severity="error", message=str(exc))],
        )

    source = LocalFileSource(path)
    issues = run_fast_checks(source, extension=path.suffix)
    checks_run += ["header_magic", "zero_tail"]

    tech_metadata: dict = {}
    if deep and not any(i.severity == "error" for i in issues):
        deep_issues, tech_metadata = run_deep_structural_check(path)
        issues += deep_issues
        checks_run.append("deep_structural")

    return IntegrityReport(
        location=str(path),
        size_bytes=source.size(),
        checks_run=checks_run,
        issues=issues,
        tech_metadata=tech_metadata,
    )
