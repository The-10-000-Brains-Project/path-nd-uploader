from unittest.mock import MagicMock

from pathnd_uploader.checksum import compute_crc32c_base64
from pathnd_uploader.gcs.uploader import upload_slide


def _record(**overrides):
    record = {
        "participant_id": "P-1",
        "stain_type": "HE",
        "slide_paths": "Collection_X/region/45122.svs",
    }
    record.update(overrides)
    return record


def test_upload_uses_slide_paths_as_object_key(tmp_path):
    slide = tmp_path / "45122.svs"
    slide.write_bytes(b"fake slide bytes")

    bucket = MagicMock()
    bucket.name = "my-bucket"
    bucket.get_blob.return_value = None
    blob = MagicMock()
    bucket.blob.return_value = blob

    result = upload_slide(bucket=bucket, slide_path=slide, record=_record())

    bucket.blob.assert_any_call("Collection_X/region/45122.svs")
    blob.upload_from_filename.assert_called_once_with(str(slide), checksum="crc32c")
    assert not result.skipped
    assert result.object_uri == "gs://my-bucket/Collection_X/region/45122.svs"


def test_upload_sets_custom_metadata(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    expected_crc32c = compute_crc32c_base64(slide)

    bucket = MagicMock()
    bucket.name = "b"
    bucket.get_blob.return_value = None
    blob = MagicMock()

    def fake_upload(*args, **kwargs):
        # Simulate the real client library refreshing blob.crc32c from the
        # server's response after a successful checksum-verified upload.
        blob.crc32c = expected_crc32c

    blob.upload_from_filename.side_effect = fake_upload
    bucket.blob.return_value = blob

    upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert blob.metadata["participant_id"] == "P-1"
    assert blob.metadata["stain_type"] == "HE"
    assert "schema_version" in blob.metadata
    assert blob.metadata["client_checksum_crc32c"] == expected_crc32c
    blob.patch.assert_called_once()


def test_upload_does_not_precompute_checksum_when_no_existing_object(tmp_path, monkeypatch):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")

    bucket = MagicMock()
    bucket.name = "b"
    bucket.get_blob.return_value = None
    blob = MagicMock()
    bucket.blob.return_value = blob

    calls = []
    monkeypatch.setattr(
        "pathnd_uploader.gcs.uploader.compute_crc32c_base64",
        lambda path: calls.append(path) or "unused",
    )

    upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert calls == []  # no local precompute pass for a first-time upload


def test_upload_skips_when_matching_object_already_exists(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"identical bytes")
    expected_crc32c = compute_crc32c_base64(slide)

    bucket = MagicMock()
    bucket.name = "b"
    existing = MagicMock()
    existing.crc32c = expected_crc32c
    bucket.get_blob.return_value = existing

    result = upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert result.skipped
    bucket.blob.assert_not_called()


def test_upload_reuploads_when_existing_checksum_differs(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"new bytes")

    bucket = MagicMock()
    bucket.name = "b"
    existing = MagicMock()
    existing.crc32c = "different-checksum"
    bucket.get_blob.return_value = existing
    blob = MagicMock()
    bucket.blob.return_value = blob

    result = upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert not result.skipped
    blob.upload_from_filename.assert_called_once()


def test_upload_records_needs_review_in_custom_metadata(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")

    bucket = MagicMock()
    bucket.name = "b"
    bucket.get_blob.return_value = None
    blob = MagicMock()
    bucket.blob.return_value = blob

    upload_slide(bucket=bucket, slide_path=slide, record=_record(), needs_review=["brain_bank_id", "stain_type"])

    assert blob.metadata["needs_review"] == "brain_bank_id,stain_type"


def test_upload_omits_needs_review_key_when_none(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")

    bucket = MagicMock()
    bucket.name = "b"
    bucket.get_blob.return_value = None
    blob = MagicMock()
    bucket.blob.return_value = blob

    upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert "needs_review" not in blob.metadata
