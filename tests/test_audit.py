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
