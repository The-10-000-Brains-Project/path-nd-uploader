"""The shared integrity checks.

Two tiers, deliberately kept separate:

* "Fast" checks (`check_header_magic`, `check_zero_tail`,
  `check_structural_completeness`, run together via `run_fast_checks`) work
  off a `ByteRangeSource` — a handful of small reads regardless of file
  size, so they're cheap enough to run against every object in a bucket
  during an audit, not just at upload time. `check_structural_completeness`
  parses the TIFF directory itself (via `tifffile`, over range-reads) for a
  hard structural fact — every tile's claimed bytes actually exist — rather
  than `check_zero_tail`'s heuristic; the two catch different corruption
  signatures and neither supersedes the other, see
  `check_structural_completeness`'s docstring.
* The "deep" structural check (`run_deep_structural_check`) opens the file
  with OpenSlide and walks its pyramid levels. This needs real random-access
  file I/O that the OpenSlide C library performs itself, so it only works
  against a local path — for GCS objects this means downloading first,
  which callers should treat as opt-in/expensive.
"""

from __future__ import annotations

from pathlib import Path

from ..retry import is_transient
from .byte_source import ByteRangeSource, RangeReadFile
from .models import IntegrityIssue

# Extensions known to be TIFF-based, where a TIFF magic-number check is meaningful.
# .mrxs (MIRAX) and .czi (Zeiss) are proprietary container formats, not TIFF.
_TIFF_MAGIC_BYTES = {
    b"II*\x00",  # little-endian classic TIFF
    b"MM\x00*",  # big-endian classic TIFF
    b"II+\x00",  # little-endian BigTIFF
    b"MM\x00+",  # big-endian BigTIFF
}
TIFF_BASED_EXTENSIONS = {".svs", ".tif", ".tiff", ".ndpi", ".scn"}

DEFAULT_SAMPLE_WINDOW_BYTES = 64 * 1024 * 1024  # 64 MiB: how much of the tail we read
# A legitimate pyramid's smallest level is often not an exact multiple of the tile size, so
# its edge tiles get zero-padded by the encoder — normal, and can be up to roughly one tile's
# worth of bytes. The absolute floor must clear that; the fraction keeps sensitivity
# proportional on very large files where a fixed absolute floor alone would be too tight.
DEFAULT_MIN_ZERO_RUN_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_MIN_ZERO_RUN_FRACTION = 0.005  # 0.5% of file size


def check_header_magic(source: ByteRangeSource, *, extension: str) -> IntegrityIssue | None:
    """Confirms the file starts with a valid TIFF magic number, when applicable."""
    if extension.lower() not in TIFF_BASED_EXTENSIONS:
        return None
    size = source.size()
    if size < 4:
        return IntegrityIssue(
            check="header_magic",
            severity="error",
            message=f"file is only {size} bytes, too small to contain a TIFF header",
        )
    header = source.read_range(0, 4)
    if header not in _TIFF_MAGIC_BYTES:
        return IntegrityIssue(
            check="header_magic",
            severity="error",
            message=f"file does not start with a recognized TIFF magic number (got {header!r})",
        )
    return None


def check_zero_tail(
    source: ByteRangeSource,
    *,
    sample_window_bytes: int = DEFAULT_SAMPLE_WINDOW_BYTES,
    min_zero_run_bytes: int = DEFAULT_MIN_ZERO_RUN_BYTES,
    min_zero_run_fraction: float = DEFAULT_MIN_ZERO_RUN_FRACTION,
) -> IntegrityIssue | None:
    """Flags a trailing run of all-zero bytes — the observed signature of an
    upload that was interrupted partway and the destination pre-allocated
    or zero-padded, leaving the pyramid directory (often written near the
    end of the file) unreachable.

    Measures the actual length of the contiguous zero run ending at EOF,
    rather than requiring the whole sampled window to be zero — a fixed
    window can be larger than the corrupted region (common on smaller
    slides), which would otherwise dilute the check with legitimate bytes
    and miss the truncation entirely. The trigger threshold is the larger of
    an absolute floor and a fraction of the file size, so a normal encoder's
    edge-tile padding on the smallest pyramid level doesn't false-positive.
    """
    size = source.size()
    if size == 0:
        return IntegrityIssue(check="zero_tail", severity="error", message="file is empty (0 bytes)")

    window_len = min(sample_window_bytes, size)
    window = source.read_range(size - window_len, window_len)
    zero_run = len(window) - len(window.rstrip(b"\x00"))
    threshold = max(min_zero_run_bytes, size * min_zero_run_fraction)

    if zero_run >= threshold:
        at_least = " (at least — the sampled window was entirely zero)" if zero_run == window_len < size else ""
        return IntegrityIssue(
            check="zero_tail",
            severity="error",
            message=(
                f"last {zero_run:,} bytes{at_least} ({zero_run / size:.1%} of the file) are "
                "zero-filled — likely a truncated/interrupted upload; the pyramid directory "
                "is probably unreachable"
            ),
        )
    return None


def check_structural_completeness(source: ByteRangeSource, *, extension: str) -> tuple[list[IntegrityIssue], dict]:
    """Parses the TIFF directory structure via range-reads — never the whole
    file — and confirms every page's tile/strip data claims to fit within
    the file's actual size. This is a structural fact, not a heuristic, but
    it catches a specific failure mode: the file being physically shorter
    than its own directory says it should be (e.g. a copy that was cut off
    mid-transfer with nothing appended after). Only meaningful for
    TIFF-based formats.

    This does NOT catch a file that was zero-padded back to its expected
    original length (the real-world case this project was built around) —
    every tile's claimed byte range still fits inside a file that size, so
    there's nothing structurally wrong to find; only the *content* of that
    region is bogus. That failure mode is exactly what `check_zero_tail`
    exists for. The two checks are complementary, not redundant — neither
    supersedes the other.

    Named `tiff_page_count` rather than reusing `level_count` for its page
    count, since a TIFF's raw page count (thumbnail/label/macro images
    included) isn't the same thing OpenSlide's `level_count` reports later
    in the deep check — the two are related but shouldn't be conflated.
    """
    if extension.lower() not in TIFF_BASED_EXTENSIONS:
        return [], {}

    import tifffile

    file_size = source.size()

    try:
        tiff = tifffile.TiffFile(RangeReadFile(source))
    except Exception as exc:  # tifffile raises a mix of its own and generic errors on malformed input
        if is_transient(exc):
            # Not a finding about the file — a network error while range-reading it.
            # Let it propagate so the caller's retry/inconclusive-classification
            # logic handles it, instead of reporting "corrupted" for a network blip.
            raise
        return (
            [
                IntegrityIssue(
                    check="structural_completeness",
                    severity="error",
                    message=f"could not parse the TIFF directory structure: {exc}",
                )
            ],
            {},
        )

    issues: list[IntegrityIssue] = []
    tech_metadata: dict = {"tiff_page_count": len(tiff.pages)}

    if len(tiff.pages) == 0:
        # tifffile is lenient: garbage after a valid magic number doesn't
        # always raise, it can just log a warning and yield zero pages —
        # that's just as unusable as a parse error and must be reported.
        issues.append(
            IntegrityIssue(
                check="structural_completeness",
                severity="error",
                message="no readable TIFF pages found — the file's directory structure appears to be corrupted",
            )
        )
        return issues, tech_metadata

    with tiff:
        for i, page in enumerate(tiff.pages):
            if page.is_tiled:
                offsets = page.tags.get("TileOffsets")
                bytecounts = page.tags.get("TileByteCounts")
            else:
                offsets = page.tags.get("StripOffsets")
                bytecounts = page.tags.get("StripByteCounts")

            if offsets is not None and bytecounts is not None:
                claimed_end = max((o + c for o, c in zip(offsets.value, bytecounts.value)), default=0)
                if claimed_end > file_size:
                    issues.append(
                        IntegrityIssue(
                            check="structural_completeness",
                            severity="error",
                            message=(
                                f"page {i}'s data claims to need up to byte {claimed_end:,}, but the "
                                f"file is only {file_size:,} bytes — the file is truncated"
                            ),
                        )
                    )

            if i == 0 and len(page.shape) >= 2:
                height, width = page.shape[0], page.shape[1]
                tech_metadata["dimensions"] = (width, height)
                if width < 512 or height < 512:
                    issues.append(
                        IntegrityIssue(
                            check="dimensions",
                            severity="error",
                            message=f"slide dimensions {(width, height)} are implausibly small for a whole-slide image",
                        )
                    )

    return issues, tech_metadata


def run_fast_checks(source: ByteRangeSource, *, extension: str) -> tuple[list[IntegrityIssue], dict]:
    issues = []
    for issue in (
        check_header_magic(source, extension=extension),
        check_zero_tail(source),
    ):
        if issue is not None:
            issues.append(issue)

    structural_issues, tech_metadata = check_structural_completeness(source, extension=extension)
    issues.extend(structural_issues)

    return issues, tech_metadata


def run_deep_structural_check(local_path: Path) -> tuple[list[IntegrityIssue], dict]:
    """Opens the slide with OpenSlide, walks every pyramid level, and reads a
    handful of full-resolution tiles. Returns (issues, tech_metadata).

    Only meaningful on a real local file — see module docstring.
    """
    try:
        import openslide
    except ImportError as exc:  # pragma: no cover - exercised only when dependency missing
        raise RuntimeError(
            "openslide-python (and the OpenSlide library, e.g. via the 'openslide-bin' "
            "package) is required for deep structural validation"
        ) from exc

    issues: list[IntegrityIssue] = []
    tech_metadata: dict = {}

    try:
        slide = openslide.OpenSlide(str(local_path))
    except openslide.OpenSlideError as exc:
        issues.append(
            IntegrityIssue(
                check="structural_open",
                severity="error",
                message=f"OpenSlide could not open the file: {exc}",
            )
        )
        return issues, tech_metadata

    with slide:
        tech_metadata = {
            "vendor": slide.properties.get("openslide.vendor"),
            "objective_power": slide.properties.get("openslide.objective-power"),
            "mpp_x": slide.properties.get("openslide.mpp-x"),
            "mpp_y": slide.properties.get("openslide.mpp-y"),
            "level_count": slide.level_count,
            "dimensions": slide.dimensions,
        }

        if slide.level_count < 1:
            issues.append(
                IntegrityIssue(check="level_count", severity="error", message="slide reports zero pyramid levels")
            )

        width, height = slide.dimensions
        if width < 512 or height < 512:
            issues.append(
                IntegrityIssue(
                    check="dimensions",
                    severity="error",
                    message=f"slide dimensions {slide.dimensions} are implausibly small for a whole-slide image",
                )
            )

        for level in range(slide.level_count):
            try:
                lw, lh = slide.level_dimensions[level]
                # Spot-check a small region near the bottom-right corner of each
                # level — the area most likely to be missing if the file was
                # truncated partway through being written.
                sample_size = 64
                x = max(lw - sample_size, 0)
                y = max(lh - sample_size, 0)
                slide.read_region((x, y), level, (min(sample_size, lw), min(sample_size, lh)))
            except openslide.OpenSlideError as exc:
                issues.append(
                    IntegrityIssue(
                        check="level_read",
                        severity="error",
                        message=f"level {level} could not be read (likely truncated): {exc}",
                    )
                )

    return issues, tech_metadata
