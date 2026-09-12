"""Tests for the file-stability guard: refuse to validate (and thus upload)
a file that's still being written — e.g. mid-copy from a scanner or network
share onto the machine running this tool. Uses a mocked clock/stat rather
than real sleeps, so these stay fast and deterministic.
"""

import os
from unittest.mock import patch

from pathnd_uploader.slide.validator import _check_file_stability, validate_slide


def _stat_result(size: int, mtime: float):
    # os.stat_result is a namedtuple-like structure; easiest to build a real
    # one via os.stat_result's positional constructor (10 stat fields).
    fields = [0] * 10
    fields[6] = size  # st_size
    fields[8] = mtime  # st_mtime
    return os.stat_result(fields)


def test_stable_file_passes(tmp_path):
    f = tmp_path / "s.svs"
    f.write_bytes(b"data")
    same = _stat_result(4, 1000.0)
    with patch("pathlib.Path.stat", return_value=same), patch("time.sleep") as mock_sleep:
        issue = _check_file_stability(f, wait_seconds=1.0)
    assert issue is None
    mock_sleep.assert_called_once_with(1.0)


def test_growing_file_is_flagged(tmp_path):
    f = tmp_path / "s.svs"
    f.write_bytes(b"data")
    before = _stat_result(1000, 1000.0)
    after = _stat_result(2000, 1005.0)  # grew during the wait window
    with patch("pathlib.Path.stat", side_effect=[before, after]), patch("time.sleep"):
        issue = _check_file_stability(f, wait_seconds=1.0)
    assert issue is not None
    assert issue.check == "file_stability"
    assert issue.severity == "error"
    assert "1,000" in issue.message and "2,000" in issue.message


def test_mtime_only_change_is_flagged(tmp_path):
    f = tmp_path / "s.svs"
    f.write_bytes(b"data")
    before = _stat_result(1000, 1000.0)
    after = _stat_result(1000, 1005.0)  # same size, but touched again mid-check
    with patch("pathlib.Path.stat", side_effect=[before, after]), patch("time.sleep"):
        issue = _check_file_stability(f, wait_seconds=1.0)
    assert issue is not None


def test_zero_wait_skips_the_check_entirely(tmp_path):
    f = tmp_path / "s.svs"
    f.write_bytes(b"data")
    with patch("time.sleep") as mock_sleep:
        issue = _check_file_stability(f, wait_seconds=0)
    assert issue is None
    mock_sleep.assert_not_called()


def test_validate_slide_short_circuits_on_an_unstable_file(clean_slide_path):
    from pathnd_uploader.integrity import IntegrityIssue

    fake_issue = IntegrityIssue(check="file_stability", severity="error", message="still changing")
    with patch("pathnd_uploader.slide.validator._check_file_stability", return_value=fake_issue):
        report = validate_slide(clean_slide_path, deep=True, stability_wait_seconds=1.0)

    assert not report.passed
    assert report.checks_run == ["file_stability"]
    assert report.issues == [fake_issue]
