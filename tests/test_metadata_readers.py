import openpyxl
import pytest

from pathnd_uploader.metadata import read_manifest, read_single_record


def _write_workbook(path):
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Slide-level_data"
    ws1.append(["participant_id", "slide_paths", "stain_type"])
    ws1.append(["P-1", "a.svs", "HE"])
    ws1.append(["P-2", "b.svs", "AT8"])

    ws2 = wb.create_sheet("Case-level_data")
    ws2.append(["participant_id", "age"])
    ws2.append(["P-1", 80])

    wb.save(path)
    return path


def test_reads_first_sheet_by_default(tmp_path):
    path = _write_workbook(tmp_path / "manifest.xlsx")
    records = read_manifest(path)
    assert len(records) == 2
    assert records[0] == {"participant_id": "P-1", "slide_paths": "a.svs", "stain_type": "HE"}


def test_reads_named_sheet(tmp_path):
    path = _write_workbook(tmp_path / "manifest.xlsx")
    records = read_manifest(path, sheet="Case-level_data")
    assert records == [{"participant_id": "P-1", "age": 80}]


def test_reads_sheet_by_index(tmp_path):
    path = _write_workbook(tmp_path / "manifest.xlsx")
    records = read_manifest(path, sheet=1)
    assert records == [{"participant_id": "P-1", "age": 80}]


def test_read_single_record_returns_the_one_row(tmp_path):
    path = tmp_path / "one.csv"
    path.write_text("participant_id,slide_paths\nP-1,a.svs\n")
    assert read_single_record(path) == {"participant_id": "P-1", "slide_paths": "a.svs"}


def test_read_single_record_rejects_multiple_rows(tmp_path):
    # No per-slide sidecar format exists — a single-slide command needs a
    # manifest with exactly one row, and must say clearly why it refused
    # anything else rather than silently picking one.
    path = tmp_path / "manifest.xlsx"
    _write_workbook(path)
    with pytest.raises(ValueError, match="exactly one record"):
        read_single_record(path)


def test_read_single_record_rejects_zero_rows(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("participant_id,slide_paths\n")
    with pytest.raises(ValueError, match="exactly one record"):
        read_single_record(path)


# A UTF-8 BOM (the three bytes EF BB BF) at the start of a file is what Excel
# and some export tools write. Real SEA-AD exports had one. With a plain utf-8
# read it stays glued to the first column name as an invisible ﻿, so a
# required field like `study` was falsely reported missing on every row.
BOM = b"\xef\xbb\xbf"


def test_csv_with_utf8_bom_reads_first_column_name_cleanly(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(BOM + b"study,slide_paths\nACT,a.svs\n")
    records = read_manifest(path)
    assert records == [{"study": "ACT", "slide_paths": "a.svs"}]
    assert "﻿study" not in records[0]


def test_json_with_utf8_bom_parses(tmp_path):
    path = tmp_path / "bom.json"
    path.write_bytes(BOM + b'[{"study": "ACT", "slide_paths": "a.svs"}]')
    assert read_manifest(path) == [{"study": "ACT", "slide_paths": "a.svs"}]


def test_jsonl_with_utf8_bom_parses(tmp_path):
    path = tmp_path / "bom.jsonl"
    path.write_bytes(BOM + b'{"study": "ACT", "slide_paths": "a.svs"}\n{"study": "ACT", "slide_paths": "b.svs"}\n')
    records = read_manifest(path)
    assert [r["slide_paths"] for r in records] == ["a.svs", "b.svs"]
    assert "study" in records[0]


def test_csv_without_bom_is_unaffected(tmp_path):
    path = tmp_path / "plain.csv"
    path.write_bytes(b"study,slide_paths\nACT,a.svs\n")
    assert read_manifest(path) == [{"study": "ACT", "slide_paths": "a.svs"}]
