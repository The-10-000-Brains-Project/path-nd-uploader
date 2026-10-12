import io
from unittest.mock import MagicMock, patch

import google.api_core.exceptions as gax_exceptions
import numpy as np
import tifffile

from pathnd_uploader.gcs.audit import audit_bucket, audit_manifest_against_bucket, audit_object


def _real_tiff_bytes() -> bytes:
    # A structurally valid (if tiny) TIFF — needed now that audit_object also
    # runs check_structural_completeness, which parses the real directory,
    # not just the magic number.
    buf = io.BytesIO()
    image = (np.random.default_rng(0).random((512, 512, 3)) * 255).astype("uint8")
    tifffile.imwrite(buf, image, photometric="rgb")
    return buf.getvalue()


def _mock_blob(name: str, data: bytes):
    blob = MagicMock()
    blob.name = name
    blob.size = len(data)
    blob.bucket.name = "test-bucket"

    def download_as_bytes(start, end):
        return data[start : end + 1]

    blob.download_as_bytes.side_effect = download_as_bytes
    return blob


def test_audit_object_flags_zero_tail():
    data = b"\xab" * 4096 + b"\x00" * (2 * 1024 * 1024)  # 2 MiB trailing zero run
    blob = _mock_blob("slide.svs", data)

    report = audit_object(blob, deep=False)

    assert not report.passed
    assert any(i.check == "zero_tail" for i in report.issues)
    assert report.location == "gs://test-bucket/slide.svs"


def test_audit_object_passes_clean_data():
    blob = _mock_blob("slide.svs", _real_tiff_bytes())

    report = audit_object(blob, deep=False)

    assert report.passed, report.issues


def test_audit_bucket_scans_all_blobs_in_parallel():
    blob1 = _mock_blob("a.svs", _real_tiff_bytes())
    blob2 = _mock_blob("b.svs", b"\xab" * 4096 + b"\x00" * (2 * 1024 * 1024))

    client = MagicMock()
    bucket = MagicMock()
    client.bucket.return_value = bucket

    with patch("pathnd_uploader.gcs.audit._iter_slide_blobs", return_value=[blob1, blob2]):
        summary = audit_bucket("test-bucket", client=client, workers=4)

    assert summary.total_scanned == 2
    assert len(summary.failed) == 1
    assert summary.failed[0].location == "gs://test-bucket/b.svs"


def test_audit_bucket_does_not_abort_when_one_object_errors():
    # A genuine, non-transient error (e.g. a bug, a permissions issue) —
    # correctly stays a real failure, not softened to "inconclusive".
    good_blob = _mock_blob("good.svs", _real_tiff_bytes())
    bad_blob = _mock_blob("bad.svs", b"irrelevant")
    bad_blob.download_as_bytes.side_effect = RuntimeError("some non-network bug")

    client = MagicMock()
    bucket = MagicMock()
    client.bucket.return_value = bucket

    with patch("pathnd_uploader.gcs.audit._iter_slide_blobs", return_value=[good_blob, bad_blob]):
        summary = audit_bucket("test-bucket", client=client, workers=4)

    assert summary.total_scanned == 2
    failed_locations = {r.location: r for r in summary.failed}
    assert "gs://test-bucket/bad.svs" in failed_locations
    bad_report = failed_locations["gs://test-bucket/bad.svs"]
    assert any(i.check == "scan_error" for i in bad_report.issues)
    assert "some non-network bug" in bad_report.issues[0].message
    assert not bad_report.is_inconclusive


def test_audit_bucket_classifies_persistent_network_errors_as_inconclusive_not_failed():
    # This is the real-world case: a genuine network error, not a finding
    # about the file. Must not be reported as "corrupted".
    good_blob = _mock_blob("good.svs", _real_tiff_bytes())
    flaky_blob = _mock_blob("flaky.svs", b"irrelevant")
    flaky_blob.download_as_bytes.side_effect = gax_exceptions.ServiceUnavailable("network is down")

    client = MagicMock()
    bucket = MagicMock()
    client.bucket.return_value = bucket

    with (
        patch("pathnd_uploader.gcs.audit._iter_slide_blobs", return_value=[good_blob, flaky_blob]),
        patch("time.sleep"),  # skip the real backoff delay between retries
    ):
        summary = audit_bucket("test-bucket", client=client, workers=4)

    assert summary.total_scanned == 2
    assert len(summary.failed) == 0  # not a real failure
    assert len(summary.inconclusive) == 1
    flaky_report = summary.inconclusive[0]
    assert flaky_report.location == "gs://test-bucket/flaky.svs"
    assert not flaky_report.passed  # still not a clean pass — needs a re-run
    assert any(i.check == "scan_incomplete" and i.severity == "inconclusive" for i in flaky_report.issues)


def test_audit_manifest_against_bucket_finds_missing_files():
    bucket = MagicMock()

    def get_blob(name):
        return None if name == "missing.svs" else MagicMock()

    bucket.get_blob.side_effect = get_blob
    client = MagicMock()
    client.bucket.return_value = bucket

    records = [{"slide_paths": "present.svs"}, {"slide_paths": "missing.svs"}]
    missing = audit_manifest_against_bucket(records, bucket_name="b", client=client)

    assert missing == ["missing.svs"]


class _FakeBucket:
    """In-memory bucket supporting blob().exists()/download_as_bytes() and list_blobs(prefix=)."""

    def __init__(self, objects: dict[str, bytes]):
        self.name = "test-bucket"
        self._objects = objects

    def blob(self, name):
        b = MagicMock()
        b.exists.side_effect = lambda: name in self._objects
        b.download_as_bytes.side_effect = lambda: self._objects[name]
        return b

    def list_blobs(self, prefix="", max_results=None):
        found = [n for n in self._objects if n.startswith(prefix)]
        return [MagicMock(name=n) for n in found[:max_results]]


def _bundle_blob(name, objects):
    blob = _mock_blob(name, objects[name])
    blob.bucket = _FakeBucket(objects)
    return blob


_SLIDEDAT = b"[DATAFILE]\nFILE_COUNT=2\nFILE_0=Data0000.dat\nFILE_1=Data0001.dat\n"
_INDEX = b"\x01" * 4096


def test_audit_mrxs_complete_bundle_passes():
    objs = {
        "run/s.mrxs": _INDEX,
        "run/s/Slidedat.ini": _SLIDEDAT,
        "run/s/Data0000.dat": b"d",
        "run/s/Data0001.dat": b"d",
        "run/s/Index.dat": b"i",
    }
    report = audit_object(_bundle_blob("run/s.mrxs", objs))
    assert report.passed, report.issues
    assert "bundle_completeness" in report.checks_run


def test_audit_mrxs_missing_data_file_is_flagged():
    objs = {
        "run/s.mrxs": _INDEX,
        "run/s/Slidedat.ini": _SLIDEDAT,
        "run/s/Data0000.dat": b"d",
        "run/s/Index.dat": b"i",
    }
    report = audit_object(_bundle_blob("run/s.mrxs", objs))
    assert [i.check for i in report.issues] == ["bundle_completeness"]
    assert "Data0001.dat" in report.issues[0].message


def test_audit_mrxs_missing_data_directory_at_bucket_root():
    report = audit_object(_bundle_blob("s.mrxs", {"s.mrxs": _INDEX}))
    assert "data directory" in report.issues[0].message


def test_audit_vms_missing_sibling_is_flagged():
    objs = {"s.vms": b"[X]\nImageFile(0,0)=a.jpg\nMapFile=m.jpg\n" + b" " * 3000, "a.jpg": b"j"}
    report = audit_object(_bundle_blob("s.vms", objs))
    assert "m.jpg" in report.issues[0].message and "a.jpg" not in report.issues[0].message


def test_audit_bundle_network_error_is_not_reported_as_a_bad_file():
    import requests

    blob = _bundle_blob("s.mrxs", {"s.mrxs": _INDEX, "s/Slidedat.ini": _SLIDEDAT})
    blob.bucket.blob = MagicMock(side_effect=requests.exceptions.ConnectionError("reset"))
    summary_report = None
    try:
        summary_report = audit_object(blob)
    except requests.exceptions.ConnectionError:
        pass  # propagates so the retry/inconclusive logic handles it
    assert summary_report is None


def test_audit_deep_runs_zero_fill_scan_on_the_downloaded_copy(tmp_path):
    import numpy as np
    import tifffile

    img = np.random.default_rng(1).integers(0, 256, (1536, 1536, 3), dtype=np.uint8)
    src = tmp_path / "src.svs"
    tifffile.imwrite(src, img, tile=(256, 256), photometric="rgb", compression="zlib")
    with tifffile.TiffFile(src) as t:
        p = t.pages[0]
        lo, hi = min(p.dataoffsets), max(o + c for o, c in zip(p.dataoffsets, p.databytecounts))
    data = bytearray(src.read_bytes())
    mid = (lo + hi) // 2 + 1
    data[mid : mid + 16 * 1024] = bytes(16 * 1024)  # too small for the sampled check to rely on
    data = bytes(data)

    blob = _mock_blob("s.svs", data)
    blob.download_to_filename.side_effect = lambda name: open(name, "wb").write(data)
    report = audit_object(blob, deep=True)
    assert "zero_filled_data" in report.checks_run
    assert any(i.check == "zero_filled_data" for i in report.issues), report.issues


class _DiskBlob:
    def __init__(self, bucket, name, data):
        self.bucket, self.name, self._data = bucket, name, data
        self.size = len(data)

    def download_to_filename(self, filename):
        with open(filename, "wb") as fh:
            fh.write(self._data)

    def download_as_bytes(self, start=None, end=None):
        return self._data if start is None else self._data[start : end + 1]

    def exists(self):
        return True


class _DiskBucket:
    name = "test-bucket"

    def __init__(self, objects):
        self._objects = objects

    def blob(self, name):
        return _DiskBlob(self, name, self._objects[name])

    def list_blobs(self, prefix="", max_results=None):
        found = [_DiskBlob(self, n, d) for n, d in sorted(self._objects.items()) if n.startswith(prefix)]
        return found[:max_results]


def _mirax_objects(prefix=""):
    from pathlib import Path

    root = Path(__file__).parent / "fixtures" / "real"
    objs = {f"{prefix}s.mrxs": (root / "CMU-1-Saved-1_16.mrxs").read_bytes()}
    for f in (root / "CMU-1-Saved-1_16").iterdir():
        objs[f"{prefix}s/{f.name}"] = f.read_bytes()
    return objs


def test_audit_deep_on_real_mirax_bundle_downloads_sidecars_and_passes():
    # Regression (found on a real bucket): deep mode downloaded only the .mrxs index, so
    # OpenSlide reported "Unsupported or missing image file" for every good MIRAX slide.
    objs = _mirax_objects("run/")
    bucket = _DiskBucket(objs)
    report = audit_object(_DiskBlob(bucket, "run/s.mrxs", objs["run/s.mrxs"]), deep=True)
    assert "deep_structural" in report.checks_run
    assert report.passed, report.issues


def test_audit_deep_bundle_download_never_writes_outside_temp_dir(tmp_path):
    objs = _mirax_objects()
    objs["s/../../escape.txt"] = b"x"
    bucket = _DiskBucket(objs)
    audit_object(_DiskBlob(bucket, "s.mrxs", objs["s.mrxs"]), deep=True)
    assert not (tmp_path.parent / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
