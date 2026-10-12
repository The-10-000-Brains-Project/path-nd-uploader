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


# --- stress-test regressions -------------------------------------------------


def _mrxs(tmp_path, ini: bytes | None, files=("Data0000.dat",), name="s"):
    f = tmp_path / f"{name}.mrxs"
    f.write_bytes(b"index")
    d = tmp_path / name
    d.mkdir()
    if ini is not None:
        (d / "Slidedat.ini").write_bytes(ini)
    for x in files:
        (d / x).parent.mkdir(parents=True, exist_ok=True)
        (d / x).write_bytes(b"d")
    return f


def _bundle_errors(path):
    return [i for i in validate_slide(path, deep=False, stability_wait_seconds=0).issues if i.check == "bundle_completeness"]


@pytest.mark.parametrize("ref", ["/etc/hosts", "../s.mrxs", "sub/../../s.mrxs", ""])
def test_mrxs_reference_escaping_data_dir_is_rejected(tmp_path, ref):
    f = _mrxs(tmp_path, f"[DATAFILE]\nFILE_COUNT=1\nFILE_0={ref}\n".encode(), files=())
    assert _bundle_errors(f)


@pytest.mark.parametrize("ini", [b"", b"[GENERAL]\nx=1\n", b"[DATAFILE]\nFILE_COUNT=0\n", b"\x00\xff\xfe garbage"])
def test_mrxs_index_listing_no_data_files_is_rejected(tmp_path, ini):
    assert _bundle_errors(_mrxs(tmp_path, ini))


def test_mrxs_file_count_mismatch_is_rejected(tmp_path):
    f = _mrxs(tmp_path, b"[DATAFILE]\nFILE_COUNT=3\nFILE_0=Data0000.dat\n")
    assert "FILE_COUNT=3" in _bundle_errors(f)[0].message


def test_mrxs_tolerates_bom_crlf_duplicate_keys_and_subdirs(tmp_path):
    ini = b"\xef\xbb\xbf[DATAFILE]\r\nFILE_COUNT=1\r\nFILE_0=sub/Data0000.dat\r\nFILE_0=sub/Data0000.dat\r\n"
    assert not _bundle_errors(_mrxs(tmp_path, ini, files=("sub/Data0000.dat",)))


def test_mrxs_data_dir_that_is_a_file_is_reported(tmp_path):
    f = tmp_path / "s.mrxs"
    f.write_bytes(b"index")
    (tmp_path / "s").write_bytes(b"not a dir")
    assert _bundle_errors(f)


def test_mrxs_unicode_and_spaces_in_name(tmp_path):
    f = _mrxs(tmp_path, b"[DATAFILE]\nFILE_COUNT=1\nFILE_0=Data0000.dat\n", name="slidé 1")
    assert not _bundle_errors(f)


def test_vms_reference_escaping_directory_is_rejected_even_if_target_exists(tmp_path):
    (tmp_path / "outside.jpg").write_bytes(b"j")
    sub = tmp_path / "sub"
    sub.mkdir()
    f = sub / "s.vms"
    f.write_text("[X]\nImageFile(0,0)=../outside.jpg\n")
    assert _bundle_errors(f)


def test_vmu_missing_ngr_and_vms_garbage_index(tmp_path):
    u = tmp_path / "u.vmu"
    u.write_text("[U]\nImageFile=u.ngr\nMapFile=m.map\n")
    (tmp_path / "m.map").write_bytes(b"m")
    assert "u.ngr" in _bundle_errors(u)[0].message
    g = tmp_path / "g.vms"
    g.write_bytes(bytes(range(256)))
    assert _bundle_errors(g)


def test_directory_named_like_a_slide_does_not_crash(tmp_path):
    d = tmp_path / "dir.svs"
    d.mkdir()
    report = validate_slide(d, stability_wait_seconds=0)
    assert not report.passed


def test_unreadable_file_is_reported_not_raised(tmp_path):
    import os

    f = tmp_path / "locked.vms"
    f.write_text("[X]\n")
    f.chmod(0)
    try:
        if os.access(f, os.R_OK):  # running as root: chmod has no effect
            pytest.skip("file remains readable (root)")
        report = validate_slide(f, stability_wait_seconds=0)
        assert any(i.check == "readable" for i in report.issues)
    finally:
        f.chmod(0o644)


def test_uppercase_and_compound_extensions_and_symlinks(clean_slide_path, tmp_path):
    for name in ("S.SVS", "s.ome.tiff", "S.NdPi"):
        p = tmp_path / name
        shutil.copy(clean_slide_path, p)
        assert validate_slide(p, deep=True, stability_wait_seconds=0).passed, name
    link = tmp_path / "link.svs"
    link.symlink_to(clean_slide_path)
    assert validate_slide(link, deep=True, stability_wait_seconds=0).passed
    broken = tmp_path / "broken.svs"
    broken.symlink_to(tmp_path / "nope")
    assert not validate_slide(broken, stability_wait_seconds=0).passed


@pytest.mark.parametrize("size", [0, 1, 3, 4, 7, 8, 15])
@pytest.mark.parametrize("ext", [".svs", ".bif", ".svslide", ".mrxs"])
def test_tiny_files_never_raise(tmp_path, ext, size):
    f = tmp_path / f"t{ext}"
    f.write_bytes(b"II*\x00BigTiff!!!!!!!"[:size])
    if ext == ".mrxs":
        (tmp_path / "t").mkdir()
    report = validate_slide(f, deep=True, stability_wait_seconds=0)
    assert not report.passed
