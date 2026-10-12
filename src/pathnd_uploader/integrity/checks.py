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

import contextlib
import logging
import os
import re
import threading
from pathlib import Path

from ..retry import is_transient
from ..formats import TIFF_BASED_EXTENSIONS, get_format
from .byte_source import ByteRangeSource, RangeReadFile
from .models import IntegrityIssue


class _TifffileWarningCapture(logging.Handler):
    """tifffile doesn't raise when the IFD chain points somewhere invalid (e.g.
    past EOF in a truncated file) — it logs a warning and silently returns the
    pages it did manage to read. That warning is the only signal, so capture it
    per-thread (audits run many files concurrently) instead of letting it go to
    stderr, where it's just noise.
    """

    _local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        sink = getattr(self._local, "sink", None)
        if sink is not None:
            sink.append(record.getMessage())

    @classmethod
    @contextlib.contextmanager
    def capture(cls):
        sink: list[str] = []
        cls._local.sink = sink
        try:
            yield sink
        finally:
            cls._local.sink = None


_tifffile_logger = logging.getLogger("tifffile")
_tifffile_logger.addHandler(_TifffileWarningCapture())
_INVALID_PAGE_OFFSET = re.compile(r"invalid page offset (\d+)")

# Header-magic and TIFF-structure applicability comes from the slide format registry.

DEFAULT_SAMPLE_WINDOW_BYTES = 64 * 1024 * 1024  # 64 MiB: how much of the tail we read
# A legitimate pyramid's smallest level is often not an exact multiple of the tile size, so
# its edge tiles get zero-padded by the encoder — normal, and can be up to roughly one tile's
# worth of bytes. The absolute floor must clear that; the fraction keeps sensitivity
# proportional on very large files where a fixed absolute floor alone would be too tight.
DEFAULT_MIN_ZERO_RUN_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_MIN_ZERO_RUN_FRACTION = 0.005  # 0.5% of file size


def check_header_magic(source: ByteRangeSource, *, extension: str) -> IntegrityIssue | None:
    """Confirms the file starts with the format's magic number (TIFF or
    BigTIFF for TIFF-based formats, SQLite for .svslide), when it has one."""
    fmt = get_format(extension)
    if fmt is None or fmt.magics is None:
        return None
    size = source.size()
    longest = max(len(m) for m in fmt.magics)
    if size < longest:
        return IntegrityIssue(
            check="header_magic",
            severity="error",
            message=f"file is only {size} bytes, too small to contain a {fmt.name} header",
        )
    header = source.read_range(0, longest)
    if not any(header.startswith(m) for m in fmt.magics):
        label = "TIFF magic number" if fmt.tiff_based else f"{fmt.name} header"
        return IntegrityIssue(
            check="header_magic",
            severity="error",
            message=f"file does not start with a recognized {label} (got {header[:4]!r})",
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
    # The absolute floor exists to clear a normal encoder's edge-tile padding (up to ~a tile),
    # but on a tiny file a 1 MiB floor is most of the file and a 30%-zeroed tail would slip
    # through. Cap the floor at 10% of the file; irrelevant above ~10 MiB.
    threshold = max(min(min_zero_run_bytes, size * 0.10), size * min_zero_run_fraction)
    # Only `window_len` bytes are ever read, so a threshold above that could never fire: the
    # 0.5% fraction exceeds the 64 MiB window on files over ~12.8 GB (a zeroed tail on a 20 GB
    # slide used to pass). Cap at half the window; legitimate padding is never that large.
    threshold = min(threshold, window_len // 2)

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

    It also catches a truncation that cuts the page-directory chain: tifffile
    reports a chain pointer past EOF only as a logged warning (the pages read
    so far still look fine), so that warning is captured and turned into an
    error. Without it, a cut that keeps page 0 intact looks like a valid
    single-page file.

    Named `tiff_page_count` rather than reusing `level_count` for its page
    count, since a TIFF's raw page count (thumbnail/label/macro images
    included) isn't the same thing OpenSlide's `level_count` reports later
    in the deep check — the two are related but shouldn't be conflated.
    """
    with _TifffileWarningCapture.capture() as warnings:
        issues, tech_metadata = _structural_completeness(source, extension=extension)

    file_size = source.size()
    for message in warnings:
        match = _INVALID_PAGE_OFFSET.search(message)
        if match:
            offset = int(match.group(1))
            where = (
                f"byte {offset:,}, past the end of the {file_size:,}-byte file — the file is truncated"
                if offset >= file_size
                else f"an invalid location (byte {offset:,})"
            )
            issues.append(
                IntegrityIssue(
                    check="structural_completeness",
                    severity="error",
                    message=f"the TIFF page directory chain points to {where}; later pages are unreachable",
                )
            )
            break
    return issues, tech_metadata


TILE_WINDOW_BYTES = 4096
MAX_TILE_SAMPLES_PER_PAGE = 24
MAX_TILE_SAMPLES_TOTAL = 64
TILE_SAMPLES_ENV = "PATHND_TILE_SAMPLES"


def _tile_sample_budget() -> tuple[int, int]:
    """(total, per-page cap) of tile windows to sample; PATHND_TILE_SAMPLES overrides the default total."""
    raw = os.environ.get(TILE_SAMPLES_ENV, "").strip()
    try:
        total = int(raw) if raw else MAX_TILE_SAMPLES_TOTAL
    except ValueError:
        total = MAX_TILE_SAMPLES_TOTAL
    total = max(1, min(total, 100_000))
    return total, max(MAX_TILE_SAMPLES_PER_PAGE, total)
_MIN_JUDGEABLE_TILE_BYTES = 64


def _sample_indices(n: int, k: int) -> list[int]:
    """Up to k indices spread evenly over range(n), always including the first and last."""
    if n <= k:
        return list(range(n))
    if k <= 1:
        return [n // 2]
    return sorted({round(j * (n - 1) / (k - 1)) for j in range(k)})


def _zero_filled_tile_windows(source: ByteRangeSource, page, offsets, bytecounts, budget: int) -> tuple[int, int]:
    """Reads a small window from the middle of a spread of tiles/strips on `page` and
    returns (windows_read, windows_that_were_entirely_zero).

    Complements check_zero_tail for zero-filled runs that are NOT at the end of the file
    (a bad copy block, a dropped chunk): a compressed tile that is entirely zero in its
    middle is never valid data, so this needs no size threshold. Uncompressed pages are
    skipped — a black region legitimately encodes as zeros there.
    """
    if int(page.compression) == 1:
        return 0, 0
    offs, counts = list(offsets), list(bytecounts)
    read = zero = 0
    cap = budget
    for idx in _sample_indices(len(offs), cap):
        off, count = int(offs[idx]), int(counts[idx])
        if count < _MIN_JUDGEABLE_TILE_BYTES or off + count > source.size():
            continue
        # NDPI (and some strip layouts) store a page as one multi-GB blob, so a single window
        # would say almost nothing about it; spread several along any large tile/strip.
        fractions = (0.2, 0.4, 0.6, 0.8) if count > 16 * TILE_WINDOW_BYTES else (0.5,)
        length = min(count, TILE_WINDOW_BYTES)
        for frac in fractions:
            if read >= cap:
                return read, zero
            start = off + min(max(int(count * frac) - length // 2, 0), count - length)
            window = source.read_range(start, length)
            read += 1
            if window and not any(window):
                zero += 1
    return read, zero


def _open_tiff(source: ByteRangeSource, extension: str):
    """Opens `source` with tifffile, naming it after the extension so tifffile picks
    the right parsing mode (see RangeReadFile). A real .ndpi is NDPI-format, but a
    standard TIFF that merely carries the .ndpi extension must not be called corrupt
    for it, so fall back to a plain parse if the NDPI parse fails.
    """
    import tifffile

    try:
        tiff = tifffile.TiffFile(RangeReadFile(source, name=f"slide{extension}"))
        if extension != ".ndpi" or len(tiff.pages) > 0:
            return tiff
        tiff.close()
    except Exception as exc:
        if extension != ".ndpi" or is_transient(exc):
            raise
    return tifffile.TiffFile(RangeReadFile(source, name="slide.tif"))


def _structural_completeness(source: ByteRangeSource, *, extension: str) -> tuple[list[IntegrityIssue], dict]:
    if extension.lower() not in TIFF_BASED_EXTENSIONS:
        return [], {}

    import tifffile

    file_size = source.size()

    try:
        tiff = _open_tiff(source, extension.lower())
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
    tech_metadata: dict = {}

    # tifffile loads pages lazily, so a truncated or corrupt directory can raise (struct.error,
    # TiffFileError, ...) anywhere below, not just in the constructor above.
    try:
        tech_metadata["tiff_page_count"] = len(tiff.pages)

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

        largest = (0, 0, 0)  # (area, width, height)
        windows_read = windows_zero = 0
        zeroed_pages: list[int] = []
        sampled_pages: list[tuple] = []
        with tiff:
            for i, page in enumerate(tiff.pages):
                # dataoffsets/databytecounts abstract over tile vs. strip layouts. Looking up
                # "TileOffsets" by name misses NDPI, which tifffile reports as tiled while the
                # file stores strip tags — the data-fits-in-file check was silently skipped there.
                offsets, bytecounts = page.dataoffsets, page.databytecounts

                # A page with no tags / no image data / no size is an IFD that was zero-filled
                # (a zeroed directory parses as an empty one rather than raising), which would
                # otherwise just look like a pyramid with fewer levels.
                if not offsets or not bytecounts or not getattr(page, "imagewidth", 0) or not getattr(page, "imagelength", 0):
                    issues.append(
                        IntegrityIssue(
                            check="structural_completeness",
                            severity="error",
                            message=f"page {i} has no image data or zero dimensions — its directory entry is empty or zero-filled",
                        )
                    )
                    continue

                if offsets and bytecounts:
                    claimed_end = max((o + c for o, c in zip(offsets, bytecounts)), default=0)
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

                if offsets and bytecounts and int(page.compression) != 1:
                    sampled_pages.append((i, page, offsets, bytecounts, sum(bytecounts)))

                width, height = getattr(page, "imagewidth", 0), getattr(page, "imagelength", 0)
                if width * height > largest[0]:
                    largest = (width * height, width, height)

        # Spread the sampling budget over pages in proportion to their data (the full-resolution
        # page holds nearly all the bytes, so it should get nearly all the windows), with at
        # least one window per page so thumbnails/labels are still looked at.
        total_bytes = sum(sp[4] for sp in sampled_pages) or 1
        sample_total, sample_page_cap = _tile_sample_budget()
        for i, page, offsets, bytecounts, nbytes in sampled_pages:
            share = max(1, round(sample_total * nbytes / total_bytes))
            read, zero = _zero_filled_tile_windows(
                source, page, offsets, bytecounts, min(share, sample_page_cap)
            )
            windows_read += read
            windows_zero += zero
            if zero:
                zeroed_pages.append(i)
        tech_metadata["tile_windows_sampled"] = windows_read
        if windows_zero:
            issues.append(
                IntegrityIssue(
                    check="zero_filled_tiles",
                    severity="error",
                    message=(
                        f"{windows_zero} of {windows_read} sampled tile windows (pages {zeroed_pages}) are entirely "
                        "zero — compressed image data is never all zeros, so a region of the file was zero-filled"
                    ),
                )
            )

        # The slide's real size is its largest page: page 0 is not always the full-resolution
        # image (Ventana BIFs lead with the label image, some SCNs with a macro), and judging
        # "implausibly small" by a label/thumbnail would reject good slides.
        if largest[0] > 0:
            _, width, height = largest
            tech_metadata["dimensions"] = (width, height)
            if width < 512 or height < 512:
                issues.append(
                    IntegrityIssue(
                        check="dimensions",
                        severity="error",
                        message=f"slide dimensions {(width, height)} are implausibly small for a whole-slide image",
                    )
                )
    except Exception as exc:
        if is_transient(exc):
            raise
        issues.append(
            IntegrityIssue(
                check="structural_completeness",
                severity="error",
                message=f"could not read the TIFF page structure (file is likely truncated or corrupted): {exc}",
            )
        )
        with contextlib.suppress(Exception):
            tiff.close()
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


# OpenSlide open errors that mean "valid file, variant this library version doesn't
# implement" rather than "damaged file". Matched as substrings of the error text.
# Ventana scanners (e.g. DP 200) emit joint direction LEFT, which OpenSlide 4.0.x rejects
# even for pristine files, so reporting it as corruption would be a false alarm.
# Leica SCN files with several ROIs at different resolutions are likewise valid but unsupported.
_OPENSLIDE_UNSUPPORTED_VARIANT_ERRORS = ("Bad direction attribute", "dissimilar main images")


ZERO_BLOCK_BYTES = 4096
_ZERO_SCAN_CHUNK_BLOCKS = 8192  # 32 MiB per read


def scan_zero_filled_data(local_path: Path, extension: str) -> tuple[list[IntegrityIssue], dict]:
    """Streams through every byte of compressed tile/strip data in a local TIFF-based
    slide and flags runs of zero-filled 4 KiB blocks. Unlike the sampled tile-window check
    (which can run over the network but only sees a fraction of the file), this is
    exhaustive — a zeroed region anywhere inside image data is found — at the cost of
    reading the whole file, so it belongs with the other local-only deep checks.

    Two or more adjacent all-zero blocks inside compressed data is never legitimate:
    JPEG/JPEG2000/LZW/deflate output has no 8 KiB runs of zeros. Uncompressed pages and
    the gaps between tiles (padding, directories) are excluded from the scan.
    """
    if extension.lower() not in TIFF_BASED_EXTENSIONS:
        return [], {}

    import numpy as np
    import tifffile

    # Open by real path (not the range-read wrapper): NDPI/BigTIFF handling then comes from the file itself.
    try:
        with tifffile.TiffFile(str(local_path)) as tiff:
            spans = []
            for page in tiff.pages:
                if int(page.compression) == 1:
                    continue
                spans.extend(zip(page.dataoffsets, page.databytecounts))
    except Exception:  # an unparseable file is reported by the structural check, not here
        return [], {}

    # Merge touching/overlapping tile ranges so we read each byte once.
    merged: list[list[int]] = []
    for start, count in sorted((int(o), int(c)) for o, c in spans if c):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], start + count)
        else:
            merged.append([start, start + count])

    block = ZERO_BLOCK_BYTES
    runs: list[tuple[int, int]] = []  # (offset, length_in_blocks)
    scanned = 0
    with open(local_path, "rb") as fh:
        for lo, hi in merged:
            first = -(-lo // block) * block  # first block fully inside the range
            last = hi // block * block
            pos, run_start, run_len = first, None, 0
            while pos < last:
                n = min(_ZERO_SCAN_CHUNK_BLOCKS, (last - pos) // block)
                fh.seek(pos)
                data = fh.read(n * block)
                n = len(data) // block
                if n == 0:
                    break
                arr = np.frombuffer(data, dtype=np.uint8, count=n * block).reshape(n, block)
                is_zero = ~arr.any(axis=1)
                scanned += n
                # collapse the boolean vector into (start, length) runs of zero blocks
                edges = np.flatnonzero(np.diff(np.concatenate(([False], is_zero, [False])).astype(np.int8)))
                for a, b in zip(edges[::2], edges[1::2]):
                    off = pos + int(a) * block
                    if run_start is not None and run_start + run_len * block == off:
                        run_len += int(b - a)
                    else:
                        if run_start is not None and run_len >= 2:
                            runs.append((run_start, run_len))
                        run_start, run_len = off, int(b - a)
                pos += n * block
                if run_start is not None and run_start + run_len * block != pos:
                    if run_len >= 2:
                        runs.append((run_start, run_len))
                    run_start, run_len = None, 0
            if run_start is not None and run_len >= 2:
                runs.append((run_start, run_len))

    metadata = {"zero_scan_bytes": scanned * block}
    if not runs:
        return [], metadata
    total = sum(r[1] for r in runs) * block
    first_off, first_len = runs[0]
    return (
        [
            IntegrityIssue(
                check="zero_filled_data",
                severity="error",
                message=(
                    f"{len(runs)} zero-filled region(s) ({total:,} bytes in all) inside compressed image data — "
                    f"first at byte {first_off:,} ({first_len * block:,} bytes); part of the file was zero-filled "
                    "(compressed image data is never all zeros)"
                ),
            )
        ],
        metadata,
    )


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
        if any(marker in str(exc) for marker in _OPENSLIDE_UNSUPPORTED_VARIANT_ERRORS):
            issues.append(
                IntegrityIssue(
                    check="structural_open",
                    severity="warning",
                    message=(
                        f"OpenSlide does not support this file variant ({exc}); deep validation could not run. "
                        "The fast checks passed, so this is not evidence of corruption"
                    ),
                )
            )
            return issues, tech_metadata
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
                # Read a small region at the corners, edges and centre of each level. The
                # bottom-right corner is the first thing missing in a truncated write, but a
                # damaged chunk elsewhere in the file only shows up if we look there too.
                sample_size = 64
                w, h = min(sample_size, lw), min(sample_size, lh)
                xs = sorted({0, max((lw - w) // 2, 0), max(lw - w, 0)})
                ys = sorted({0, max((lh - h) // 2, 0), max(lh - h, 0)})
                for y in ys:
                    for x in xs:
                        slide.read_region((x, y), level, (w, h))
            except openslide.OpenSlideError as exc:
                issues.append(
                    IntegrityIssue(
                        check="level_read",
                        severity="error",
                        message=f"level {level} could not be read (likely truncated or damaged): {exc}",
                    )
                )

    return issues, tech_metadata
