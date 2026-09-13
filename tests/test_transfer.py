from unittest.mock import MagicMock, patch

import google.api_core.exceptions as gax_exceptions

from pathnd_uploader.gcs.transfer import _remap_key, transfer_bucket, transfer_object
from pathnd_uploader.integrity import IntegrityIssue, IntegrityReport


def _passing_report():
    return IntegrityReport(location="gs://source-bucket/slide.svs", size_bytes=1000, checks_run=["zero_tail"], issues=[])


def _failing_report():
    return IntegrityReport(
        location="gs://source-bucket/slide.svs",
        size_bytes=1000,
        checks_run=["zero_tail"],
        issues=[IntegrityIssue(check="zero_tail", severity="error", message="corrupted")],
    )


def _source_blob(name="Collection_X/slide.svs", bucket_name="source-bucket"):
    blob = MagicMock()
    blob.name = name
    blob.bucket.name = bucket_name
    return blob


def test_transfer_object_skips_copy_when_validation_fails():
    source = _source_blob()
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    dest_bucket.get_blob.return_value = None  # nothing already at the destination

    with patch("pathnd_uploader.gcs.transfer._audit_object_with_retry", return_value=_failing_report()):
        result = transfer_object(source, dest_bucket)

    assert not result.copied
    dest_bucket.blob.assert_not_called()


def test_transfer_object_copies_via_rewrite_loop_when_validation_passes():
    source = _source_blob()
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    dest_bucket.get_blob.return_value = None
    dest_blob = MagicMock()
    dest_blob.metadata = None
    dest_bucket.blob.return_value = dest_blob
    # First call returns a continuation token (large object, not done in one call); second completes it.
    dest_blob.rewrite.side_effect = [("tok1", 100, 1000), (None, 1000, 1000)]

    with patch("pathnd_uploader.gcs.transfer._audit_object_with_retry", return_value=_passing_report()):
        result = transfer_object(source, dest_bucket)

    assert result.copied
    assert result.dest_uri == "gs://dest-bucket/Collection_X/slide.svs"
    assert result.source_uri == "gs://source-bucket/Collection_X/slide.svs"
    assert dest_blob.rewrite.call_count == 2
    dest_blob.rewrite.assert_any_call(source, token=None)
    dest_blob.rewrite.assert_any_call(source, token="tok1")


def test_transfer_object_stamps_provenance_metadata():
    source = _source_blob()
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    dest_bucket.get_blob.return_value = None
    dest_blob = MagicMock()
    dest_blob.metadata = {"existing": "value"}
    dest_bucket.blob.return_value = dest_blob
    dest_blob.rewrite.return_value = (None, 1000, 1000)

    with patch("pathnd_uploader.gcs.transfer._audit_object_with_retry", return_value=_passing_report()):
        transfer_object(source, dest_bucket)

    assert dest_blob.metadata["existing"] == "value"  # preserved, not clobbered
    assert dest_blob.metadata["transferred_from"] == "gs://source-bucket/Collection_X/slide.svs"
    assert "transferred_at" in dest_blob.metadata
    dest_blob.patch.assert_called_once()


def test_transfer_object_uses_explicit_dest_key():
    source = _source_blob()
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    dest_bucket.get_blob.return_value = None
    dest_blob = MagicMock()
    dest_blob.metadata = {}
    dest_bucket.blob.return_value = dest_blob
    dest_blob.rewrite.return_value = (None, 1000, 1000)

    with patch("pathnd_uploader.gcs.transfer._audit_object_with_retry", return_value=_passing_report()):
        result = transfer_object(source, dest_bucket, dest_key="new/path/slide.svs")

    dest_bucket.blob.assert_called_once_with("new/path/slide.svs")
    assert result.dest_uri == "gs://dest-bucket/new/path/slide.svs"


def test_transfer_object_skips_when_already_present_with_matching_crc32c():
    # Idempotent resume: the destination already holds a byte-identical copy,
    # so no re-copy (and no re-paid egress), and the source isn't even validated.
    source = _source_blob()
    source.crc32c = "abc123=="
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    existing = MagicMock()
    existing.crc32c = "abc123=="
    dest_bucket.get_blob.return_value = existing

    with patch("pathnd_uploader.gcs.transfer._audit_object_with_retry") as mock_audit:
        result = transfer_object(source, dest_bucket)

    assert result.already_present
    assert not result.copied
    assert result.in_destination
    mock_audit.assert_not_called()  # no source re-read
    dest_bucket.blob.assert_not_called()  # no rewrite


def test_transfer_object_copies_when_dest_exists_but_crc32c_differs():
    # A partial/corrupt earlier copy (different checksum) must NOT be treated
    # as already-present — it gets re-copied.
    source = _source_blob()
    source.crc32c = "aaaa=="
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    existing = MagicMock()
    existing.crc32c = "bbbb=="
    dest_bucket.get_blob.return_value = existing
    dest_blob = MagicMock()
    dest_blob.metadata = {}
    dest_bucket.blob.return_value = dest_blob
    dest_blob.rewrite.return_value = (None, 1, 1)

    with patch("pathnd_uploader.gcs.transfer._audit_object_with_retry", return_value=_passing_report()):
        result = transfer_object(source, dest_bucket)

    assert result.copied
    assert not result.already_present
    dest_blob.rewrite.assert_called()


def test_remap_key_no_dest_prefix_keeps_same_key():
    assert _remap_key("Collection_X/a.svs", prefix="", dest_prefix=None) == "Collection_X/a.svs"


def test_remap_key_replaces_matching_prefix():
    assert (
        _remap_key("Collection_X/sub/a.svs", prefix="Collection_X", dest_prefix="Collection_Y")
        == "Collection_Y/sub/a.svs"
    )


def test_remap_key_falls_back_when_key_does_not_start_with_prefix():
    assert _remap_key("other/a.svs", prefix="Collection_X", dest_prefix="Collection_Y") == "Collection_Y/other/a.svs"


def test_transfer_bucket_iterates_source_blobs_and_remaps_dest_keys():
    blob1 = _source_blob(name="Collection_X/a.svs")
    blob2 = _source_blob(name="Collection_X/b.svs")

    client = MagicMock()
    source_bucket = MagicMock()
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    client.bucket.side_effect = lambda name: source_bucket if name == "src" else dest_bucket

    dest_bucket.get_blob.return_value = None
    dest_blob = MagicMock()
    dest_blob.metadata = {}
    dest_bucket.blob.return_value = dest_blob
    dest_blob.rewrite.return_value = (None, 1, 1)

    with (
        patch("pathnd_uploader.gcs.transfer._iter_slide_blobs", return_value=[blob1, blob2]),
        patch("pathnd_uploader.gcs.transfer._audit_object_with_retry", return_value=_passing_report()),
    ):
        summary = transfer_bucket("src", "dst", prefix="Collection_X", dest_prefix="Collection_Y", client=client)

    assert len(summary.results) == 2
    assert summary.results[0].dest_uri == "gs://dest-bucket/Collection_Y/a.svs"
    assert summary.results[1].dest_uri == "gs://dest-bucket/Collection_Y/b.svs"
    assert len(summary.copied) == 2
    assert len(summary.skipped) == 0


def test_transfer_bucket_skips_failing_objects_but_continues():
    blob_bad = _source_blob(name="Collection_X/bad.svs")
    blob_good = _source_blob(name="Collection_X/good.svs")

    client = MagicMock()
    source_bucket = MagicMock()
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    client.bucket.side_effect = lambda name: source_bucket if name == "src" else dest_bucket

    dest_bucket.get_blob.return_value = None
    dest_blob = MagicMock()
    dest_blob.metadata = {}
    dest_bucket.blob.return_value = dest_blob
    dest_blob.rewrite.return_value = (None, 1, 1)

    def fake_audit(blob, deep=False):
        return _failing_report() if blob.name.endswith("bad.svs") else _passing_report()

    with (
        patch("pathnd_uploader.gcs.transfer._iter_slide_blobs", return_value=[blob_bad, blob_good]),
        patch("pathnd_uploader.gcs.transfer._audit_object_with_retry", side_effect=fake_audit),
    ):
        summary = transfer_bucket("src", "dst", client=client)

    assert len(summary.copied) == 1
    assert len(summary.skipped) == 1
    assert summary.skipped[0].source_uri.endswith("bad.svs")


def test_transfer_bucket_classifies_persistent_network_errors_as_inconclusive():
    flaky_blob = _source_blob(name="Collection_X/flaky.svs")

    client = MagicMock()
    source_bucket = MagicMock()
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    client.bucket.side_effect = lambda name: source_bucket if name == "src" else dest_bucket
    dest_bucket.get_blob.return_value = None

    with (
        patch("pathnd_uploader.gcs.transfer._iter_slide_blobs", return_value=[flaky_blob]),
        patch(
            "pathnd_uploader.gcs.transfer._audit_object_with_retry",
            side_effect=gax_exceptions.ServiceUnavailable("network is down"),
        ),
        patch("time.sleep"),
    ):
        summary = transfer_bucket("src", "dst", client=client)

    assert len(summary.copied) == 0
    assert len(summary.skipped) == 0  # not a real failure either
    assert len(summary.inconclusive) == 1
    result = summary.inconclusive[0]
    assert not result.copied
    assert any(i.check == "transfer_incomplete" and i.severity == "inconclusive" for i in result.integrity_report.issues)


def test_transfer_bucket_counts_already_present_separately():
    blob = _source_blob(name="Collection_X/a.svs")
    blob.crc32c = "match=="

    client = MagicMock()
    source_bucket = MagicMock()
    dest_bucket = MagicMock()
    dest_bucket.name = "dest-bucket"
    client.bucket.side_effect = lambda name: source_bucket if name == "src" else dest_bucket

    existing = MagicMock()
    existing.crc32c = "match=="
    dest_bucket.get_blob.return_value = existing

    with (
        patch("pathnd_uploader.gcs.transfer._iter_slide_blobs", return_value=[blob]),
        patch("pathnd_uploader.gcs.transfer._audit_object_with_retry") as mock_audit,
    ):
        summary = transfer_bucket("src", "dst", client=client)

    assert len(summary.copied) == 0
    assert len(summary.already_present) == 1
    assert len(summary.skipped) == 0
    mock_audit.assert_not_called()
