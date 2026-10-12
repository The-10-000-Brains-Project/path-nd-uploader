"""Zero-filled regions inside compressed image data: the sampled tile-window check (works over
a range-read source) and the exhaustive local scan (--deep)."""

import numpy as np
import pytest
import tifffile

from pathnd_uploader.integrity.byte_source import LocalFileSource
from pathnd_uploader.integrity.checks import check_structural_completeness, scan_zero_filled_data
from pathnd_uploader.slide.validator import validate_slide


def _compressed_tiff(path, *, side=2048, tile=256, compression="zlib"):
    rng = np.random.default_rng(1)
    # noisy so compressed tiles are incompressible-ish (no legitimate long zero runs)
    img = rng.integers(0, 256, (side, side, 3), dtype=np.uint8)
    with tifffile.TiffWriter(path) as tif:
        tif.write(img, tile=(tile, tile), photometric="rgb", compression=compression, subfiletype=0)
        tif.write(img[::2, ::2], tile=(tile, tile), photometric="rgb", compression=compression, subfiletype=1)
    return path


def _zero(path, start, length):
    data = bytearray(path.read_bytes())
    data[start : start + length] = bytes(length)
    path.write_bytes(bytes(data))


def _data_span(path):
    with tifffile.TiffFile(path) as t:
        p = t.pages[0]
        return min(p.dataoffsets), max(o + c for o, c in zip(p.dataoffsets, p.databytecounts))


@pytest.fixture
def clean(tmp_path):
    return _compressed_tiff(tmp_path / "clean.svs")


def test_clean_compressed_slide_has_no_zero_findings(clean):
    assert scan_zero_filled_data(clean, ".svs")[0] == []
    assert not [i for i in check_structural_completeness(LocalFileSource(clean), extension=".svs")[0]]


@pytest.mark.parametrize("length", [12 * 1024, 64 * 1024, 1024 * 1024])
def test_exhaustive_scan_finds_zeroed_run_inside_tile_data(clean, length):
    lo, hi = _data_span(clean)
    _zero(clean, (lo + hi) // 2, length)
    issues, meta = scan_zero_filled_data(clean, ".svs")
    assert [i.check for i in issues] == ["zero_filled_data"]
    assert meta["zero_scan_bytes"] > 0


def test_exhaustive_scan_ignores_runs_shorter_than_two_blocks(clean):
    lo, hi = _data_span(clean)
    _zero(clean, (lo + hi) // 2, 4000)  # < one block: can't fill two aligned 4 KiB blocks
    assert scan_zero_filled_data(clean, ".svs")[0] == []


def test_exhaustive_scan_skips_uncompressed_pages(tmp_path):
    # An uncompressed page legitimately contains zero runs (black regions); never flag those.
    f = tmp_path / "raw.svs"
    img = np.zeros((1024, 1024, 3), dtype=np.uint8)
    tifffile.imwrite(f, img, tile=(256, 256), photometric="rgb")
    assert scan_zero_filled_data(f, ".svs")[0] == []
    f2 = tmp_path / "raw2.svs"
    tifffile.imwrite(f2, img, tile=(256, 256), photometric="rgb")
    assert check_structural_completeness(LocalFileSource(f2), extension=".svs")[0] == []


def test_non_tiff_extension_not_scanned(tmp_path):
    f = tmp_path / "x.mrxs"
    f.write_bytes(bytes(100_000))
    assert scan_zero_filled_data(f, ".mrxs") == ([], {})


def test_sampled_window_check_catches_a_large_zeroed_region_without_reading_everything(clean):
    lo, hi = _data_span(clean)
    _zero(clean, lo, (hi - lo) // 2)  # half the first page's tile data
    issues, meta = check_structural_completeness(LocalFileSource(clean), extension=".svs")
    assert any(i.check == "zero_filled_tiles" for i in issues), issues
    assert meta["tile_windows_sampled"] > 0


def test_validate_slide_deep_reports_zeroed_run_that_fast_sampling_could_miss(clean):
    lo, hi = _data_span(clean)
    _zero(clean, (lo + hi) // 2 + 1, 16 * 1024)
    fast = validate_slide(clean, deep=False, stability_wait_seconds=0)
    deep = validate_slide(clean, deep=True, stability_wait_seconds=0)
    assert any(i.check == "zero_filled_data" for i in deep.issues), deep.issues
    assert "zero_filled_data" in deep.checks_run and "zero_filled_data" not in fast.checks_run


def test_tiny_tiles_are_not_judged(tmp_path):
    # Tiles < 64 bytes (e.g. an all-white tile compressed to almost nothing) can't be judged.
    f = tmp_path / "white.svs"
    img = np.full((1024, 1024, 3), 255, dtype=np.uint8)
    tifffile.imwrite(f, img, tile=(256, 256), photometric="rgb", compression="zlib")
    assert check_structural_completeness(LocalFileSource(f), extension=".svs")[0] == []
    assert scan_zero_filled_data(f, ".svs")[0] == []
