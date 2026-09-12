from pathnd_uploader.batch import BatchItem, items_from_manifest, process_batch_item, run_batch


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
