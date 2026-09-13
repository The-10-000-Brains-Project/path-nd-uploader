"""Pre-upload validation of a local slide file.

Thin orchestration over the shared `integrity` core: run the fast checks
first (cheap, catch the common truncated-upload case immediately), and only
run the expensive deep OpenSlide walk if those pass — no point spending
seconds opening a pyramid we already know is truncated.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..integrity import IntegrityIssue, IntegrityReport, LocalFileSource, run_deep_structural_check, run_fast_checks
from .formats import UnsupportedSlideFormat, validate_extension

DEFAULT_STABILITY_WAIT_SECONDS = 1.0


def _check_file_stability(path: Path, *, wait_seconds: float) -> IntegrityIssue | None:
    """Guards against validating (and then uploading) a file that's still
    being written — e.g. mid-copy from a scanner or network share onto the
    machine running this tool. Such a file isn't necessarily corrupted yet,
    just incomplete, and neither the fast nor deep checks are guaranteed to
    catch that: it may not have a zero-filled tail (there's simply nothing
    there yet), and the deep check's tile spot-checks might land on a region
    already written. Comparing size+mtime before/after a short wait is a
    cheap, reliable way to detect "still changing right now".

    Not applied to GCS objects during an audit — a completed object there is
    immutable by definition, so this race doesn't exist for that path.
    """
    if wait_seconds <= 0:
        return None
    before = path.stat()
    time.sleep(wait_seconds)
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
        return IntegrityIssue(
            check="file_stability",
            severity="error",
            message=(
                f"file size or modification time changed during a {wait_seconds}s stability check "
                f"({before.st_size:,} -> {after.st_size:,} bytes) — it appears to still be mid-copy; "
                "re-run once it has finished being written"
            ),
        )
    return None


def validate_slide(
    path: Path, *, deep: bool = True, stability_wait_seconds: float = DEFAULT_STABILITY_WAIT_SECONDS
) -> IntegrityReport:
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

    stability_issue = _check_file_stability(path, wait_seconds=stability_wait_seconds)
    if stability_issue is not None:
        return IntegrityReport(
            location=str(path),
            size_bytes=path.stat().st_size,
            checks_run=["file_stability"],
            issues=[stability_issue],
        )
    checks_run.append("file_stability")

    source = LocalFileSource(path)
    issues, tech_metadata = run_fast_checks(source, extension=path.suffix)
    checks_run += ["header_magic", "zero_tail", "structural_completeness"]

    if deep and not any(i.severity == "error" for i in issues):
        deep_issues, deep_metadata = run_deep_structural_check(path)
        issues += deep_issues
        tech_metadata = {**tech_metadata, **deep_metadata}
        checks_run.append("deep_structural")

    return IntegrityReport(
        location=str(path),
        size_bytes=source.size(),
        checks_run=checks_run,
        issues=issues,
        tech_metadata=tech_metadata,
    )
