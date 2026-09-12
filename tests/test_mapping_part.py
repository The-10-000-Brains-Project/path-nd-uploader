"""Tests for the PART (Mount Sinai) mapping profile.

Uses small synthetic rows shaped like the real export rather than the real
spreadsheet itself — unlike BDR's source CSV (whose clinical columns were
all empty), this one is fully populated with real participant-level
genetic/clinical data, so it isn't committed to the repo. The values below
mirror real observed patterns (confirmed against the live 4,503-row export
and cross-checked against the actual bucket listing) without being real
participant records.
"""

from pathnd_uploader.mapping import apply_profile
from pathnd_uploader.mapping.part import PART_PROFILE, _normalize_id, _to_gcs_relative_path
from pathnd_uploader.metadata import validate_metadata


def _row(**overrides):
    row = {
        "SLIDE_IDS": "42312",
        "BLOCK_IDS": "H",
        "STAINS": "AT8",
        "SLIDE_PATHS": "W:\\Collection_PART_DATA_MINERVA\\Hippocampus_AT8_stain\\42312.svs",
        "FileSizes": "249.79",
        "PWG_ID": "1001",
        "NEW_PWG_ID": "1001_21_0",
        "ADC_BBID": "113452",
        "CENTER_NAME": "University of Pennsylvania",
    }
    row.update(overrides)
    return row


def test_clean_row_maps_and_validates():
    mapped = apply_profile(_row(), PART_PROFILE)
    assert mapped["participant_id"] == "1001_21_0"
    assert mapped["brain_bank_id"] == "113452"
    assert mapped["stain_type"] == "AT8"
    assert mapped["slide_paths"] == "Collection_PART/Hippocampus_AT8_stain/42312.svs"
    assert mapped["study"] == "PART"
    assert validate_metadata(mapped).is_valid


def test_prefers_new_pwg_id_over_pwg_id():
    mapped = apply_profile(_row(NEW_PWG_ID="1001_21_0", PWG_ID="1001"), PART_PROFILE)
    assert mapped["participant_id"] == "1001_21_0"


def test_falls_back_to_pwg_id_when_new_pwg_id_missing():
    mapped = apply_profile(_row(NEW_PWG_ID=None), PART_PROFILE)
    assert mapped["participant_id"] == "1001"


def test_excel_float_stringified_id_is_normalized():
    mapped = apply_profile(_row(ADC_BBID="113452.0"), PART_PROFILE)
    assert mapped["brain_bank_id"] == "113452"


def test_literal_none_string_is_treated_as_missing():
    mapped = apply_profile(_row(ADC_BBID="None"), PART_PROFILE)
    assert "brain_bank_id" not in mapped
    assert not validate_metadata(mapped).is_valid


def test_literal_nan_string_is_treated_as_missing():
    mapped = apply_profile(_row(ADC_BBID="nan"), PART_PROFILE)
    assert "brain_bank_id" not in mapped


def test_missing_stain_is_a_real_gap_not_papered_over():
    mapped = apply_profile(_row(STAINS=None), PART_PROFILE)
    assert "stain_type" not in mapped
    result = validate_metadata(mapped)
    assert not result.is_valid
    assert any(e.field == "stain_type" for e in result.errors)


def test_truncated_slide_path_does_not_produce_a_broken_key():
    # Observed real pattern on "Batch Stain Controls..." rows: the source
    # spreadsheet's own SLIDE_PATHS value is truncated mid-folder-name.
    mapped = apply_profile(
        _row(SLIDE_PATHS="W:\\Collection_PART_DATA_MINERVA\\Batch Stain Controls (LFB H&E"),
        PART_PROFILE,
    )
    assert "slide_paths" not in mapped
    assert not validate_metadata(mapped).is_valid


def test_to_gcs_relative_path_matches_verified_live_bucket_layout():
    # Spot-checked against gs://pathnd_mtsinai_us_east4/Collection_PART/ directly.
    assert (
        _to_gcs_relative_path("W:\\Collection_PART_DATA_MINERVA\\Hippocampus_LFB_HE\\47412.svs")
        == "Collection_PART/Hippocampus_LFB_HE/47412.svs"
    )


def test_to_gcs_relative_path_rejects_non_slide_extension():
    assert _to_gcs_relative_path("W:\\Collection_PART_DATA_MINERVA\\Hippocampus_AT8_stain\\46464.xml") is None


def test_normalize_id_handles_observed_sentinel_shapes():
    assert _normalize_id("113452.0") == "113452"
    assert _normalize_id("113452") == "113452"
    assert _normalize_id("None") is None
    assert _normalize_id("nan") is None
    assert _normalize_id(None) is None
    assert _normalize_id("") is None
