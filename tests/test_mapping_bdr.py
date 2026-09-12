from pathlib import Path

from pathnd_uploader.mapping import apply_profile
from pathnd_uploader.mapping.bdr import BDR_PROFILE
from pathnd_uploader.metadata import read_manifest, validate_metadata

FIXTURE = Path(__file__).parent / "fixtures" / "BDR_Slides_metadata.csv"


def test_maps_donor_id_and_injects_study():
    raw = {"donor_id": "20243839", "slide_name": "20243839_B_HE_Preview_Image.tif"}
    mapped = apply_profile(raw, BDR_PROFILE)
    assert mapped["participant_id"] == "20243839"
    assert mapped["study"] == "BDR"
    assert "donor_id" not in mapped  # renamed away, not duplicated


def test_extracts_slide_paths_and_unambiguous_stain():
    raw = {"donor_id": "1", "slide_name": "1_B_a-SYN_Overview_Image.tif"}
    mapped = apply_profile(raw, BDR_PROFILE)
    assert mapped["slide_paths"] == "1_B_a-SYN_Overview_Image.tif"
    assert mapped["stain_type"] == "aSyn"


def test_leaves_ambiguous_stain_tokens_unmapped():
    # These require a domain judgment call (which antibody clone / phospho
    # state) that shouldn't be guessed silently.
    for slide_name, raw_token in [
        ("1_B_4G8_Preview_Image.tif", "4G8"),
        ("1_B_TDP43_Preview_Image.tif", "TDP43"),
        ("1_B_P-TDP43_Preview_Image.tif", "P-TDP43"),
    ]:
        mapped = apply_profile({"donor_id": "1", "slide_name": slide_name}, BDR_PROFILE)
        assert mapped["stain_type"] == raw_token


def test_handles_double_underscore_missing_block():
    # Real observed row shape: donor followed by an empty block field.
    raw = {"donor_id": "20243807", "slide_name": "20243807__H_AT8_Preview_Image.tif"}
    mapped = apply_profile(raw, BDR_PROFILE)
    assert mapped["stain_type"] == "AT8"  # not "H" (the block letter)


def test_handles_fused_donor_and_block():
    raw = {"donor_id": "20218933B", "slide_name": "20218933B_LFB_Preview_Image.tif"}
    mapped = apply_profile(raw, BDR_PROFILE)
    assert mapped["stain_type"] == "LFB"


def test_unmapped_raw_columns_are_preserved_for_visibility():
    raw = {"donor_id": "1", "slide_name": "1_B_HE_Preview_Image.tif", "GENDER": "M"}
    mapped = apply_profile(raw, BDR_PROFILE)
    assert mapped["GENDER"] == "M"  # not dropped, just not validated


def test_real_export_fixture_maps_and_validates_as_expected():
    records = read_manifest(FIXTURE)
    assert len(records) == 980

    mapped_records = [apply_profile(r, BDR_PROFILE) for r in records]
    results = [
        validate_metadata(r, downgrade_to_warning=BDR_PROFILE.known_gaps) for r in mapped_records
    ]

    # brain_bank_id (always) and stain_type (only when ambiguous) are known,
    # unfixable-by-mapping gaps in this export — flagged as needs_review, not
    # blocking errors. Nothing should still be a hard error: mapping resolves
    # participant_id/slide_paths/study, and known_gaps covers the rest.
    assert all(r.is_valid for r in results)
    assert all(not r.errors for r in results)

    review_field_sets = [frozenset(r.field for r in result.needs_review) for result in results]
    assert all("brain_bank_id" in fields for fields in review_field_sets)
    assert sum("stain_type" in fields for fields in review_field_sets) == 240
    assert sum(fields == frozenset({"brain_bank_id"}) for fields in review_field_sets) == 740

    # every mapped record has slide_paths pointing at its own slide_name
    for raw, mapped in zip(records, mapped_records):
        assert mapped["slide_paths"] == raw["slide_name"]
