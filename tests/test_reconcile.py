from pathlib import Path

from pathnd_uploader.reconcile import check_slide_path_exists, reconcile


def test_check_slide_path_exists_flags_missing_file(tmp_path):
    record = {"slide_paths": "does/not/exist.svs"}
    missing = tmp_path / "does" / "not" / "exist.svs"
    error = check_slide_path_exists(record, missing)
    assert error is not None
    assert error.field == "slide_paths"


def test_check_slide_path_exists_passes_when_file_present(tmp_path):
    p = tmp_path / "slide.svs"
    p.write_bytes(b"data")
    record = {"slide_paths": "slide.svs"}
    assert check_slide_path_exists(record, p) is None


def test_reconcile_flags_uploaded_file_mismatch(tmp_path):
    record = {"slide_paths": "expected.svs"}
    result = reconcile(record, uploaded_path=Path("actually_uploaded.svs"), tech_metadata={})
    assert not result.is_valid
    assert any(e.field == "slide_paths" for e in result.errors)


def test_reconcile_warns_on_magnification_mismatch(tmp_path):
    record = {"slide_paths": "slide.svs", "scanner_objective_magnification": "40"}
    result = reconcile(record, uploaded_path=Path("slide.svs"), tech_metadata={"objective_power": "20"})
    assert result.is_valid  # warning only, not an error
    assert any(w.field == "scanner_objective_magnification" for w in result.warnings)


def test_reconcile_passes_on_matching_magnification():
    record = {"slide_paths": "slide.svs", "scanner_objective_magnification": "40"}
    result = reconcile(record, uploaded_path=Path("slide.svs"), tech_metadata={"objective_power": "40"})
    assert result.is_valid
    assert not result.warnings
