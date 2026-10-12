"""Tests for check_structural_completeness — parses the TIFF directory via
range-reads (never the whole file) and confirms every page's tile/strip
data actually fits within the file's real size. Catches a file that's
physically shorter than its own directory claims; does NOT catch a file
zero-padded back to its original expected length (see check_zero_tail for
that case) — the two checks are complementary, not overlapping.
"""

import google.api_core.exceptions as gax_exceptions
import pytest

from pathnd_uploader.integrity.byte_source import LocalFileSource
from pathnd_uploader.integrity.checks import check_structural_completeness


class _FlakySource:
    """A ByteRangeSource whose reads fail with a transient network error —
    simulates a connection drop mid-parse, distinct from actually-corrupted
    content.
    """

    def __init__(self, size: int):
        self._size = size

    def size(self) -> int:
        return self._size

    def read_range(self, start: int, length: int) -> bytes:
        raise gax_exceptions.ServiceUnavailable("connection dropped")

    def describe(self) -> str:
        return "flaky://test"


def test_transient_error_during_parse_is_not_swallowed_as_a_finding():
    # Must propagate, not get reported as "could not parse the TIFF
    # directory structure" — that would misleadingly imply the file itself
    # is bad, when this is just a network error.
    source = _FlakySource(size=10_000_000)
    with pytest.raises(gax_exceptions.ServiceUnavailable):
        check_structural_completeness(source, extension=".svs")


def test_clean_file_has_no_issues(clean_slide_path):
    issues, tech_metadata = check_structural_completeness(LocalFileSource(clean_slide_path), extension=".svs")
    assert issues == []
    assert tech_metadata["tiff_page_count"] == 4
    assert tech_metadata["dimensions"] == (1024, 1024)


def test_catches_plain_truncation_that_zero_tail_would_miss(bare_truncated_slide_path):
    # No zero-padding here — just cut short. This is the case the zero-tail
    # heuristic structurally cannot detect.
    issues, _ = check_structural_completeness(LocalFileSource(bare_truncated_slide_path), extension=".svs")
    assert len(issues) >= 1
    assert all(i.check == "structural_completeness" for i in issues)
    assert all(i.severity == "error" for i in issues)


def test_zero_padded_truncation_at_original_size_is_not_this_checks_job(truncated_slide_path):
    # Important distinction: truncated_slide_path keeps the file at its
    # ORIGINAL total size (the tail is zero-filled, not removed), so every
    # tile's claimed byte range still fits — this check has no way to know
    # those bytes were overwritten with zeros rather than real data. That
    # failure mode is exactly what check_zero_tail exists for; the two
    # checks are complementary, not redundant, and this documents why both
    # are needed rather than either superseding the other.
    issues, _ = check_structural_completeness(LocalFileSource(truncated_slide_path), extension=".svs")
    assert issues == []


def test_non_tiff_extension_is_skipped(tmp_path):
    f = tmp_path / "slide.mrxs"
    f.write_bytes(b"not a tiff at all")
    issues, tech_metadata = check_structural_completeness(LocalFileSource(f), extension=".mrxs")
    assert issues == []
    assert tech_metadata == {}


def test_garbage_content_with_tiff_extension_reports_parse_failure(tmp_path):
    f = tmp_path / "slide.svs"
    f.write_bytes(b"II*\x00" + b"garbage, not a real TIFF directory" * 20)
    issues, tech_metadata = check_structural_completeness(LocalFileSource(f), extension=".svs")
    assert len(issues) == 1
    assert issues[0].check == "structural_completeness"
    assert issues[0].severity == "error"
    assert tech_metadata == {"tiff_page_count": 0}


def test_implausibly_small_dimensions_are_flagged(tmp_path):
    import numpy as np
    import tifffile

    f = tmp_path / "tiny.svs"
    small = (np.random.default_rng(0).random((64, 64, 3)) * 255).astype("uint8")
    tifffile.imwrite(f, small, photometric="rgb")

    issues, tech_metadata = check_structural_completeness(LocalFileSource(f), extension=".svs")
    assert any(i.check == "dimensions" for i in issues)
    assert tech_metadata["dimensions"] == (64, 64)


def test_truncation_that_cuts_only_the_page_chain_is_caught(tmp_path):
    # Regression (found on a real Aperio .svs): layout is IFD0, level-0 tile data, then
    # IFD1... A cut after level-0's data leaves page 0 fully valid, and tifffile only *logs*
    # that the next-IFD pointer is past EOF — it returns one good-looking page.
    import numpy as np
    import tifffile

    full = tmp_path / "full.svs"
    arr = (np.random.default_rng(0).random((512, 512, 3)) * 255).astype("uint8")
    with tifffile.TiffWriter(full) as tif:
        tif.write(arr, tile=(128, 128), photometric="rgb")
        tif.write(arr[::2, ::2], photometric="rgb", subfiletype=1)
        tif.write(arr[::4, ::4], photometric="rgb", subfiletype=1)
    with tifffile.TiffFile(full) as t:
        p0 = t.pages[0]
        page0_end = max(o + c for o, c in zip(p0.dataoffsets, p0.databytecounts))
    cut = tmp_path / "cut.svs"
    cut.write_bytes(full.read_bytes()[:page0_end])

    issues, _ = check_structural_completeness(LocalFileSource(cut), extension=".svs")
    assert any("page directory chain" in i.message and "truncated" in i.message for i in issues), issues
    assert check_structural_completeness(LocalFileSource(full), extension=".svs")[0] == []
