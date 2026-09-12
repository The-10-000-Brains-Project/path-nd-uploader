from pathnd_uploader.slide.validator import validate_slide

# stability_wait_seconds=0 everywhere below: these fixtures are static files with no
# concurrent writer, so the real check would pass anyway, but skipping the wait keeps
# the suite fast. The check itself has dedicated tests in test_file_stability.py.


def test_clean_slide_passes_deep_validation(clean_slide_path):
    report = validate_slide(clean_slide_path, deep=True, stability_wait_seconds=0)
    assert report.passed, report.issues
    assert report.tech_metadata["level_count"] == 4
    assert report.tech_metadata["vendor"] == "generic-tiff"


def test_truncated_slide_is_caught_by_fast_check_alone(truncated_slide_path):
    # deep=False: only the fast (zero-tail/header) checks run, matching what
    # a bucket audit does by default without downloading the object.
    report = validate_slide(truncated_slide_path, deep=False, stability_wait_seconds=0)
    assert not report.passed
    assert any(i.check == "zero_tail" for i in report.issues)


def test_truncated_slide_is_caught_with_deep_validation_too(truncated_slide_path):
    report = validate_slide(truncated_slide_path, deep=True, stability_wait_seconds=0)
    assert not report.passed


def test_missing_file_is_reported():
    report = validate_slide("/nonexistent/path/slide.svs", stability_wait_seconds=0)
    assert not report.passed
    assert any(i.check == "path_exists" for i in report.issues)


def test_unsupported_extension_is_rejected(tmp_path):
    bad = tmp_path / "slide.docx"
    bad.write_bytes(b"not a slide")
    report = validate_slide(bad, stability_wait_seconds=0)
    assert not report.passed
    assert any(i.check == "extension" for i in report.issues)
