from pathnd_uploader.schema import load_schema


def test_loads_expected_required_fields():
    schema = load_schema()
    required = set(schema.required_field_names)
    assert {"participant_id", "brain_bank_id", "slide_paths", "stain_type"} <= required


def test_field_types_parsed():
    schema = load_schema()
    stain = schema.get("stain_type")
    assert stain.type == "enum"
    assert "HE" in stain.allowed_values
    assert stain.required is True

    pmi = schema.get("postmortem_interval_hours")
    assert pmi.type == "float"
    assert pmi.constraints == {"min": 0.0, "max": 98.9}
    assert pmi.required is False
    assert pmi.nullable is True


def test_version_is_pinned():
    schema = load_schema()
    assert schema.version
    assert schema.source_commit
