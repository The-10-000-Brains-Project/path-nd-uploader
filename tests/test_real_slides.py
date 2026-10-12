"""Regression tests on real vendor files (see fixtures/real/README.md).
Synthetic fixtures can't reproduce vendor layouts; these did expose real bugs."""

import shutil
from pathlib import Path

import pytest

from pathnd_uploader.slide.validator import validate_slide

REAL = Path(__file__).parent / "fixtures" / "real"
SVS = REAL / "CMU-1-Small-Region.svs"
MRXS = REAL / "CMU-1-Saved-1_16.mrxs"


def _validate(path, deep=True):
    return validate_slide(path, deep=deep, stability_wait_seconds=0)


def test_real_aperio_passes_fast_and_deep():
    report = _validate(SVS)
    assert report.passed, report.issues
    assert report.tech_metadata["vendor"] == "aperio"


@pytest.mark.parametrize("keep", [0.5, 0.7, 0.99])
def test_real_aperio_bare_truncation_is_caught_by_fast_checks(tmp_path, keep):
    data = SVS.read_bytes()
    cut = tmp_path / "cut.svs"
    cut.write_bytes(data[: int(len(data) * keep)])
    assert not _validate(cut, deep=False).passed


@pytest.mark.parametrize("keep", [0.5, 0.7])
def test_real_aperio_zero_padded_tail_is_caught(tmp_path, keep):
    data = SVS.read_bytes()
    cut = tmp_path / "zeroed.svs"
    cut.write_bytes(data[: int(len(data) * keep)] + bytes(len(data) - int(len(data) * keep)))
    assert any(i.check == "zero_tail" for i in _validate(cut, deep=False).issues)


def test_real_mirax_bundle_passes_fast_and_deep():
    report = _validate(MRXS)
    assert report.passed, report.issues
    assert report.tech_metadata["vendor"] == "mirax"


@pytest.mark.parametrize("victim", ["Data0003.dat", "Index.dat", "Slidedat.ini"])
def test_real_mirax_bundle_with_missing_file_is_caught(tmp_path, victim):
    shutil.copytree(REAL / "CMU-1-Saved-1_16", tmp_path / "CMU-1-Saved-1_16")
    shutil.copy(MRXS, tmp_path / MRXS.name)
    (tmp_path / "CMU-1-Saved-1_16" / victim).unlink()
    report = _validate(tmp_path / MRXS.name, deep=False)
    assert any(i.check == "bundle_completeness" and victim in i.message for i in report.issues)
