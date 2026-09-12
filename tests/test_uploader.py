import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pathnd_uploader.checksum import compute_crc32c_base64
from pathnd_uploader.gcs.uploader import _gcloud_storage_cp, upload_slide


def test_gcloud_storage_cp_invokes_expected_command(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("pathnd_uploader.gcs.uploader.subprocess.run", return_value=fake_result) as mock_run:
        _gcloud_storage_cp(slide, "gs://bucket/key.svs")

    mock_run.assert_called_once_with(
        ["gcloud", "storage", "cp", str(slide), "gs://bucket/key.svs"],
        capture_output=True,
        text=True,
    )


def test_gcloud_storage_cp_raises_with_stderr_on_failure(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="ERROR: (gcloud.storage.cp) Reauthentication is needed.\n"
    )

    with patch("pathnd_uploader.gcs.uploader.subprocess.run", return_value=fake_result):
        with pytest.raises(RuntimeError, match="Reauthentication is needed"):
            _gcloud_storage_cp(slide, "gs://bucket/key.svs")


def _record(**overrides):
    record = {
        "participant_id": "P-1",
        "stain_type": "HE",
        "slide_paths": "Collection_X/region/45122.svs",
    }
    record.update(overrides)
    return record


def _bucket_with_blob(name="b"):
    bucket = MagicMock()
    bucket.name = name
    bucket.get_blob.return_value = None
    blob = MagicMock()
    bucket.blob.return_value = blob
    return bucket, blob


def test_upload_uses_slide_paths_as_object_key(tmp_path):
    slide = tmp_path / "45122.svs"
    slide.write_bytes(b"fake slide bytes")
    bucket, blob = _bucket_with_blob("my-bucket")

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp") as mock_cp:
        result = upload_slide(bucket=bucket, slide_path=slide, record=_record())

    bucket.blob.assert_any_call("Collection_X/region/45122.svs")
    mock_cp.assert_called_once_with(slide, "gs://my-bucket/Collection_X/region/45122.svs")
    blob.reload.assert_called_once()
    assert not result.skipped
    assert result.object_uri == "gs://my-bucket/Collection_X/region/45122.svs"


def test_upload_sets_custom_metadata(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    expected_crc32c = compute_crc32c_base64(slide)

    bucket, blob = _bucket_with_blob()
    blob.crc32c = expected_crc32c  # set as if gcloud storage cp's transfer already happened

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp"):
        upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert blob.metadata["participant_id"] == "P-1"
    assert blob.metadata["stain_type"] == "HE"
    assert "schema_version" in blob.metadata
    assert blob.metadata["client_checksum_crc32c"] == expected_crc32c
    blob.patch.assert_called_once()


def test_upload_does_not_precompute_checksum_when_no_existing_object(tmp_path, monkeypatch):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    bucket, blob = _bucket_with_blob()

    calls = []
    monkeypatch.setattr(
        "pathnd_uploader.gcs.uploader.compute_crc32c_base64",
        lambda path: calls.append(path) or "unused",
    )

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp"):
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

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp") as mock_cp:
        result = upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert result.skipped
    bucket.blob.assert_not_called()
    mock_cp.assert_not_called()


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

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp") as mock_cp:
        result = upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert not result.skipped
    mock_cp.assert_called_once_with(slide, "gs://b/Collection_X/region/45122.svs")


def test_upload_records_needs_review_in_custom_metadata(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    bucket, blob = _bucket_with_blob()

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp"):
        upload_slide(bucket=bucket, slide_path=slide, record=_record(), needs_review=["brain_bank_id", "stain_type"])

    assert blob.metadata["needs_review"] == "brain_bank_id,stain_type"


def test_upload_omits_needs_review_key_when_none(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    bucket, blob = _bucket_with_blob()

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp"):
        upload_slide(bucket=bucket, slide_path=slide, record=_record())

    assert "needs_review" not in blob.metadata


def test_upload_propagates_gcloud_cp_failure(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    bucket, blob = _bucket_with_blob()

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            upload_slide(bucket=bucket, slide_path=slide, record=_record())
    blob.patch.assert_not_called()  # never gets to metadata stamping if the transfer failed


def test_upload_extra_files_go_through_gcloud_storage_cp(tmp_path):
    slide = tmp_path / "s.svs"
    slide.write_bytes(b"bytes")
    extra = tmp_path / "extra.dat"
    extra.write_bytes(b"extra bytes")
    bucket, blob = _bucket_with_blob("b")

    with patch("pathnd_uploader.gcs.uploader._gcloud_storage_cp") as mock_cp:
        upload_slide(bucket=bucket, slide_path=slide, record=_record(), extra_files=[extra])

    calls = [c.args for c in mock_cp.call_args_list]
    assert (slide, "gs://b/Collection_X/region/45122.svs") in calls
    assert any(c[0] == extra and c[1].endswith("extra.dat") for c in calls)