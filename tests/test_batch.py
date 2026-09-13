from pathnd_uploader.batch import (
    BatchItem,
    discover_slides_in_directory,
    items_from_manifest,
    process_batch_item,
    process_item,
    run_batch,
)


def test_items_from_manifest_handles_unresolvable_slide_paths(tmp_path):
    # Real observed case (Mount Sinai PART export): some rows have no
    # slide_paths at all, or a value a source profile couldn't map. This
    # must not raise — it's a per-item failure, not a batch-ending crash.
    records = [{"slide_paths": "a.svs"}, {"participant_id": "no-file-for-this-one"}]
    items = items_from_manifest(records, manifest_dir=tmp_path)

    assert len(items) == 2
    assert items[0].slide_path == tmp_path / "a.svs"
    assert items[1].slide_path is None


def test_process_batch_item_reports_unresolved_slide_path_as_a_failure():
    item = BatchItem(slide_path=None, record={"slide_paths": "missing.svs"})
    result = process_batch_item(item)

    assert not result.passed
    assert result.error is not None
    assert "missing.svs" in result.slide_path


def test_run_batch_does_not_abort_on_an_unresolvable_item(tmp_path):
    good = tmp_path / "good.svs"
    good.write_bytes(b"II*\x00" + b"\x00" * 10)  # will fail integrity checks, but that's a separate concern
    items = [
        BatchItem(slide_path=good, record={"slide_paths": "good.svs"}),
        BatchItem(slide_path=None, record={"slide_paths": "missing.svs"}),
    ]

    results = run_batch(items, deep=False, stability_wait_seconds=0)

    assert len(results) == 2
    unresolved = [r for r in results if r.error]
    assert len(unresolved) == 1
    assert "missing.svs" in unresolved[0].slide_path


def test_discover_slides_in_directory_attaches_no_metadata(tmp_path):
    # No per-slide sidecar format exists — a directory scan is
    # integrity-only, never metadata-validated, regardless of what other
    # files happen to sit alongside the slides.
    (tmp_path / "a.svs").write_bytes(b"II*\x00")
    (tmp_path / "a.json").write_text('{"participant_id": "should be ignored"}')
    (tmp_path / "notes.txt").write_text("not a slide")

    items = discover_slides_in_directory(tmp_path)

    assert len(items) == 1
    assert items[0].slide_path == tmp_path / "a.svs"
    assert items[0].record is None


def test_process_item_with_no_record_is_integrity_only(clean_slide_path):
    result = process_item(clean_slide_path, None, deep=False, stability_wait_seconds=0)

    assert result.metadata_result is None
    assert result.reconciliation is None
    assert result.integrity_report is not None


def test_process_item_with_a_record_validates_metadata_too(clean_slide_path):
    record = {"participant_id": "P-1", "slide_paths": clean_slide_path.name}
    result = process_item(clean_slide_path, record, deep=False, stability_wait_seconds=0)

    assert result.metadata_result is not None
    assert not result.metadata_result.is_valid  # missing brain_bank_id/study/stain_type
