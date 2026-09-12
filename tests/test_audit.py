from unittest.mock import MagicMock

from pathnd_uploader.gcs.audit import audit_manifest_against_bucket, audit_object


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
    data = b"II*\x00" + (b"\xab" * 4096)
    blob = _mock_blob("slide.svs", data)

    report = audit_object(blob, deep=False)

    assert report.passed


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
