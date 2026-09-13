from pathnd_uploader.integrity.byte_source import LocalFileSource
from pathnd_uploader.integrity.checks import check_header_magic, check_zero_tail, run_fast_checks


def _write(tmp_path, name, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_zero_tail_detects_trailing_zero_run(tmp_path):
    # 1KB of real data followed by 2KB of zeros; thresholds set small so the
    # test doesn't need a multi-MB fixture to exercise the logic.
    data = b"\xab" * 1024 + b"\x00" * 2048
    source = LocalFileSource(_write(tmp_path, "f.bin", data))
    issue = check_zero_tail(source, min_zero_run_bytes=512, min_zero_run_fraction=0.01)
    assert issue is not None
    assert issue.check == "zero_tail"
    assert issue.severity == "error"


def test_zero_tail_ignores_small_run_under_threshold(tmp_path):
    # Only 8 trailing zero bytes — normal end-of-structure padding, not a truncation.
    data = b"\xab" * 4096 + b"\x00" * 8
    source = LocalFileSource(_write(tmp_path, "f.bin", data))
    issue = check_zero_tail(source, min_zero_run_bytes=512, min_zero_run_fraction=0.01)
    assert issue is None


def test_zero_tail_window_smaller_than_corrupted_region_still_detects_it(tmp_path):
    # Regression test: a fixed sample window bigger than the real zero run used to
    # dilute the check with legitimate bytes and miss the truncation. Here the
    # corrupted region (3KB) is *larger* than the sample window (1KB), so the whole
    # sampled window is zero and must still be flagged.
    data = b"\xab" * 1024 + b"\x00" * 3072
    source = LocalFileSource(_write(tmp_path, "f.bin", data))
    issue = check_zero_tail(source, sample_window_bytes=1024, min_zero_run_bytes=512, min_zero_run_fraction=0.01)
    assert issue is not None


def test_zero_tail_window_larger_than_corrupted_region_is_not_diluted(tmp_path):
    # The corrupted region (200 bytes) is *smaller* than the sample window
    # (4096 bytes); the measured run length must still equal the true 200
    # bytes, not "the whole window wasn't all zero so nothing is wrong."
    data = b"\xab" * 4096 + b"\x00" * 200
    source = LocalFileSource(_write(tmp_path, "f.bin", data))
    issue = check_zero_tail(source, sample_window_bytes=4096, min_zero_run_bytes=100, min_zero_run_fraction=0.0)
    assert issue is not None
    assert "200" in issue.message


def test_zero_tail_empty_file(tmp_path):
    source = LocalFileSource(_write(tmp_path, "empty.bin", b""))
    issue = check_zero_tail(source)
    assert issue is not None
    assert "empty" in issue.message


def test_header_magic_accepts_valid_tiff(tmp_path):
    source = LocalFileSource(_write(tmp_path, "f.svs", b"II*\x00" + b"\x00" * 100))
    assert check_header_magic(source, extension=".svs") is None


def test_header_magic_rejects_bad_header(tmp_path):
    source = LocalFileSource(_write(tmp_path, "f.svs", b"NOTA" + b"\x00" * 100))
    issue = check_header_magic(source, extension=".svs")
    assert issue is not None
    assert issue.check == "header_magic"


def test_header_magic_skipped_for_non_tiff_formats(tmp_path):
    # .mrxs is not TIFF-based; the magic check shouldn't apply to it.
    source = LocalFileSource(_write(tmp_path, "f.mrxs", b"NOTA" + b"\x00" * 100))
    assert check_header_magic(source, extension=".mrxs") is None


def test_run_fast_checks_returns_issues_and_tech_metadata(clean_slide_path):
    source = LocalFileSource(clean_slide_path)
    issues, tech_metadata = run_fast_checks(source, extension=".svs")
    assert issues == []
    assert tech_metadata["tiff_page_count"] == 4
    assert tech_metadata["dimensions"] == (1024, 1024)


def test_run_fast_checks_aggregates_issues_from_all_three_checks(bare_truncated_slide_path):
    source = LocalFileSource(bare_truncated_slide_path)
    issues, _ = run_fast_checks(source, extension=".svs")
    assert any(i.check == "structural_completeness" for i in issues)
