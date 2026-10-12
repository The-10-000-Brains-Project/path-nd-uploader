import shutil

import pytest

from pathnd_uploader.formats import SUPPORTED_EXTENSIONS, UnsupportedSlideFormat, validate_extension
from pathnd_uploader.integrity.byte_source import LocalFileSource
from pathnd_uploader.integrity.checks import check_header_magic, check_structural_completeness
from pathnd_uploader.slide.validator import validate_slide

TIFF_EXTENSIONS = [".svs", ".ndpi", ".scn", ".bif", ".tif", ".tiff"]


@pytest.mark.parametrize("ext", TIFF_EXTENSIONS)
def test_clean_tiff_based_slide_passes_fast_and_deep(clean_slide_path, tmp_path, ext):
    path = tmp_path / f"slide{ext}"
    shutil.copy(clean_slide_path, path)
    report = validate_slide(path, deep=True, stability_wait_seconds=0)
    assert report.passed, report.issues
    assert report.tech_metadata["tiff_page_count"] == 4


@pytest.mark.parametrize("ext", TIFF_EXTENSIONS)
def test_truncated_tiff_based_slide_fails_for_every_format(bare_truncated_slide_path, tmp_path, ext):
    path = tmp_path / f"slide{ext}"
    shutil.copy(bare_truncated_slide_path, path)
    report = validate_slide(path, deep=False, stability_wait_seconds=0)
    assert any(i.check == "structural_completeness" for i in report.issues)


def test_bif_gets_tiff_header_and_structure_checks(tmp_path):
    f = tmp_path / "slide.bif"
    f.write_bytes(b"NOTATIFF" * 64)
    assert check_header_magic(LocalFileSource(f), extension=".bif").check == "header_magic"
    issues, _ = check_structural_completeness(LocalFileSource(f), extension=".bif")
    assert issues


def test_svslide_checks_sqlite_magic(tmp_path):
    good = tmp_path / "good.svslide"
    good.write_bytes(b"SQLite format 3\x00" + b"\x01" * 64)
    bad = tmp_path / "bad.svslide"
    bad.write_bytes(b"II*\x00" + b"\x01" * 64)
    assert check_header_magic(LocalFileSource(good), extension=".svslide") is None
    assert check_header_magic(LocalFileSource(bad), extension=".svslide") is not None


@pytest.mark.parametrize("ext", [".czi", ".isyntax", ".dcm"])
def test_known_but_unsupported_formats_are_rejected_with_a_clear_message(tmp_path, ext):
    f = tmp_path / f"slide{ext}"
    f.write_bytes(b"x" * 16)
    with pytest.raises(UnsupportedSlideFormat, match="recognized pathology format"):
        validate_extension(f)
    report = validate_slide(f, stability_wait_seconds=0)
    assert not report.passed
    assert report.issues[0].check == "extension"


def test_unknown_extension_is_rejected(tmp_path):
    with pytest.raises(UnsupportedSlideFormat, match="not a supported WSI format"):
        validate_extension(tmp_path / "slide.xyz")


def test_registry_covers_documented_extensions():
    assert {".svs", ".ndpi", ".scn", ".bif", ".tif", ".tiff", ".mrxs", ".vms", ".vmu", ".svslide"} <= SUPPORTED_EXTENSIONS


def test_mrxs_missing_data_directory_is_reported(tmp_path):
    f = tmp_path / "slide.mrxs"
    f.write_bytes(b"index")
    report = validate_slide(f, stability_wait_seconds=0)
    assert [i.check for i in report.issues] == ["bundle_completeness"]
    assert "data directory" in report.issues[0].message


def test_mrxs_missing_referenced_datafile_is_reported(tmp_path):
    f = tmp_path / "slide.mrxs"
    f.write_bytes(b"index")
    data = tmp_path / "slide"
    data.mkdir()
    (data / "Slidedat.ini").write_text("[DATAFILE]\nFILE_COUNT = 2\nFILE_0 = Data0000.dat\nFILE_1 = Data0001.dat\n")
    (data / "Data0000.dat").write_bytes(b"d")
    report = validate_slide(f, stability_wait_seconds=0)
    assert [i.check for i in report.issues] == ["bundle_completeness"]
    assert "Data0001.dat" in report.issues[0].message
    assert "Data0000.dat" not in report.issues[0].message


def test_mrxs_complete_bundle_passes_bundle_check(tmp_path):
    f = tmp_path / "slide.mrxs"
    f.write_bytes(b"index")
    data = tmp_path / "slide"
    data.mkdir()
    (data / "Slidedat.ini").write_text("[DATAFILE]\nFILE_COUNT = 1\nFILE_0 = Data0000.dat\n")
    (data / "Data0000.dat").write_bytes(b"d")
    report = validate_slide(f, deep=False, stability_wait_seconds=0)
    assert "bundle_completeness" in report.checks_run
    assert not any(i.check == "bundle_completeness" for i in report.issues)


def test_vms_missing_referenced_image_is_reported(tmp_path):
    f = tmp_path / "slide.vms"
    f.write_text("[Virtual Microscope Specimen]\nImageFile(0,0)=slide_0_0.jpg\nImageFile(0,1)=slide_0_1.jpg\n")
    (tmp_path / "slide_0_0.jpg").write_bytes(b"j")
    report = validate_slide(f, stability_wait_seconds=0)
    assert [i.check for i in report.issues] == ["bundle_completeness"]
    assert "slide_0_1.jpg" in report.issues[0].message
