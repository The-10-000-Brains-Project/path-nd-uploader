"""Source profile for Mount Sinai's PART collection metadata
(`Crary-Lab_10KBrains_PART_metadata.xlsx`, "Slide-level_data" sheet).

Reverse-engineered against the real 4,503-row export currently in
`gs://pathnd_mtsinai_us_east4/PART_Metadata/`. Unlike BDR's raw export
(see `bdr.py`), this one is already close to CDE-shaped:

* `STAINS` values (`AT8`, `Hirano`, `Beta-Amy`, `LFB/H&E`) are already an
  exact match for the schema's `stain_type` enum — no value translation
  needed, just a column rename.
* `ADC_BBID` ("Biobank ID assigned by participating institutions") is a
  real, populated `brain_bank_id` equivalent — unlike BDR, this field
  genuinely exists here.
* `SLIDE_PATHS` is a Windows UNC-style *local* path
  (`W:\\Collection_PART_DATA_MINERVA\\Hippocampus_AT8_stain\\42312.svs`)
  used during upload from Mount Sinai's side, not the GCS object key
  (`Collection_PART/Hippocampus_AT8_stain/42312.svs`). `_to_gcs_relative_path`
  does that translation and was verified against the live bucket listing.

Known, real data-quality issues in the source file that this profile
deliberately does NOT paper over:

* ~5.5% of rows have no `STAINS` value, ~0.4% have no `SLIDE_PATHS` value
  at all (mostly rows the sheet's own `NOTES_Why_no_sections` column
  explains, e.g. "HIPPOCAMPUS NOT ON HARD DRIVE") — these are real,
  per-row gaps, not something a mapping layer can invent, so they are
  left to fail validation normally (not added to `known_gaps`).
* ~177 rows are batch/QC control slides (`BLOCK_IDS == "Control"`,
  `CENTER_NAME == "CONTROL"`), already self-flagged by the source file's
  own `valid_pwg == False` column. This profile does not filter them out
  (mapping shouldn't silently drop rows) — they correctly fail on missing
  `brain_bank_id`/`study` context, which is honest: they aren't real
  participant specimens.
* `ADC_BBID` and other numeric-looking fields are inconsistently
  formatted — some are clean integer strings ("15701"), some are
  Excel-float-stringified ("113452.0"), and ~12% are the literal text
  "None"/"nan" rather than a true empty cell. `_normalize_id` handles all
  three for `brain_bank_id` specifically.
"""

from __future__ import annotations

from .base import SourceProfile

_LOCAL_ROOT_PREFIX = "w:/collection_part_data_minerva/"
_GCS_COLLECTION_ROOT = "Collection_PART"

_NULL_SENTINELS = {"none", "nan", "n/a", "na", ""}


def _normalize_id(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULL_SENTINELS:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _to_gcs_relative_path(windows_path: str) -> str | None:
    if not windows_path:
        return None
    normalized = windows_path.replace("\\", "/")
    if normalized.lower().startswith(_LOCAL_ROOT_PREFIX):
        rest = normalized[len(_LOCAL_ROOT_PREFIX) :]
    else:
        rest = normalized.lstrip("/")
    gcs_path = f"{_GCS_COLLECTION_ROOT}/{rest}"
    # Guards against the source file's occasional truncated paths (observed
    # on "Batch Stain Controls..." rows) — if it doesn't end in a slide
    # extension, don't emit a silently-broken path.
    if not gcs_path.lower().endswith((".svs", ".tif", ".tiff", ".ndpi", ".scn")):
        return None
    return gcs_path


def _derive(raw: dict) -> dict:
    derived: dict = {}

    participant_id = _normalize_id(raw.get("NEW_PWG_ID")) or _normalize_id(raw.get("PWG_ID"))
    if participant_id:
        derived["participant_id"] = participant_id

    brain_bank_id = _normalize_id(raw.get("ADC_BBID"))
    if brain_bank_id:
        derived["brain_bank_id"] = brain_bank_id

    slide_paths = _to_gcs_relative_path(raw.get("SLIDE_PATHS"))
    if slide_paths:
        derived["slide_paths"] = slide_paths

    stain = _normalize_id(raw.get("STAINS"))
    if stain:
        derived["stain_type"] = stain

    return derived


PART_PROFILE = SourceProfile(
    name="part",
    constants={"study": "PART"},
    derive=_derive,
)
