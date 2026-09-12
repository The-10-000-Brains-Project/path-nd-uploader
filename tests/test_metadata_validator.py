from pathnd_uploader.metadata import validate_metadata


def valid_record(**overrides):
    record = {
        "participant_id": "P-00231",
        "brain_bank_id": "BB-4471",
        "study": "Path-ND",
        "slide_paths": "Collection_PART/Frontal_Hirano/45122.svs",
        "stain_type": "HE",
        "scanner_manufacturer": "Leica",
        "scanner_objective_magnification": "40",
        "postmortem_interval_hours": 12.5,
    }
    record.update(overrides)
    return record


def test_valid_record_passes():
    result = validate_metadata(valid_record())
    assert result.is_valid, result.errors


def test_missing_required_field_is_an_error():
    record = valid_record()
    del record["participant_id"]
    result = validate_metadata(record)
    assert not result.is_valid
    assert any(e.field == "participant_id" for e in result.errors)


def test_bad_enum_value_is_an_error():
    result = validate_metadata(valid_record(stain_type="not-a-real-stain"))
    assert not result.is_valid
    assert any(e.field == "stain_type" for e in result.errors)


def test_out_of_range_numeric_is_an_error():
    result = validate_metadata(valid_record(postmortem_interval_hours=500))
    assert not result.is_valid
    assert any(e.field == "postmortem_interval_hours" for e in result.errors)


def test_unknown_field_is_a_warning_by_default():
    result = validate_metadata(valid_record(totally_made_up_field="x"))
    assert result.is_valid
    assert any(w.field == "totally_made_up_field" for w in result.warnings)


def test_unknown_field_is_an_error_in_strict_mode():
    result = validate_metadata(valid_record(totally_made_up_field="x"), strict=True)
    assert not result.is_valid
    assert any(e.field == "totally_made_up_field" for e in result.errors)


def test_empty_required_field_is_an_error():
    result = validate_metadata(valid_record(participant_id=""))
    assert not result.is_valid
    assert any(e.field == "participant_id" for e in result.errors)


def test_downgrade_to_warning_moves_error_to_needs_review():
    record = valid_record()
    del record["participant_id"]
    result = validate_metadata(record, downgrade_to_warning=frozenset({"participant_id"}))
    assert result.is_valid  # doesn't block
    assert not any(e.field == "participant_id" for e in result.errors)
    assert any(r.field == "participant_id" for r in result.needs_review)


def test_downgrade_to_warning_does_not_affect_other_fields():
    record = valid_record()
    del record["participant_id"]
    del record["brain_bank_id"]
    result = validate_metadata(record, downgrade_to_warning=frozenset({"participant_id"}))
    assert not result.is_valid  # brain_bank_id is still a real, blocking error
    assert any(e.field == "brain_bank_id" for e in result.errors)
    assert any(r.field == "participant_id" for r in result.needs_review)
