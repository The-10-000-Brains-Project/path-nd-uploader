"""Source profile for the BDR (Brain Data Resource) brain bank's raw export.

Reverse-engineered against a real 980-row export
(`BDR_Slides_metadata.csv`): each (donor, block, stain) combination
produces exactly four `slide_name` files, one per `KNOWN_KINDS` suffix —
confirmed exhaustively (0 unmatched rows, exactly 245 combinations x 4
kinds = 980).

Known, honest gaps this profile does NOT paper over:

* `brain_bank_id` is a required CDE field with no populated source column
  anywhere in the export (`BBNId`, `AutopsyId`, and the `BDR Code in *`
  columns are 100% empty across all 980 rows) — that's a real data gap for
  BDR to resolve, not something a mapping layer can invent.
* Several extracted stain tokens (`4G8`/`4GB`, and the `TDP43` family) are
  deliberately left unmapped rather than guessed — see
  `_CONFIDENT_STAIN_MAP` below.

Both of those are declared in `known_gaps` below: still surfaced (as
`needs_review`, not silently dropped), but they don't block validation or
upload — they're gaps in the source data itself, not something the archive
can fix by re-running this tool differently.
* Whether the three auxiliary image kinds (label/overview/preview) should
  be exempted from the deep structural check's pyramid/dimension floor
  (they're likely single-resolution thumbnails, not WSI pyramids) is an
  open question — untested here since no actual slide files were provided,
  only this metadata CSV.
"""

from __future__ import annotations

from pathlib import Path

from .base import SourceProfile

KNOWN_KINDS = ("LabelArea_Image", "Overview_Image", "Preview_Image", "Default_Extended")

# Only exact case/punctuation variants of an otherwise-unambiguous schema enum
# value are auto-mapped here. Antibody/stain codes that require domain judgment
# (is "4G8"/"4GB" aBeta? is bare "TDP43" the phosphorylated or
# non-phosphorylated form?) are deliberately left unmapped so validate_metadata
# surfaces them by their raw token for a human reviewer, rather than guessed.
_CONFIDENT_STAIN_MAP = {
    "a-syn": "aSyn",
}


def _extract_stain(slide_name: str) -> str | None:
    stem = Path(slide_name).stem
    for kind in KNOWN_KINDS:
        suffix = f"_{kind}"
        if stem.lower().endswith(suffix.lower()):
            prefix_tokens = stem[: -len(suffix)].split("_")
            return prefix_tokens[-1] if prefix_tokens and prefix_tokens[-1] else None
    return None


def _derive(raw: dict) -> dict:
    derived: dict = {}
    slide_name = raw.get("slide_name")
    if not slide_name:
        return derived

    derived["slide_paths"] = slide_name

    raw_stain = _extract_stain(slide_name)
    if raw_stain:
        derived["stain_type"] = _CONFIDENT_STAIN_MAP.get(raw_stain.lower(), raw_stain)

    return derived


BDR_PROFILE = SourceProfile(
    name="bdr",
    column_renames={"donor_id": "participant_id"},
    constants={"study": "BDR"},
    derive=_derive,
    known_gaps=frozenset({"brain_bank_id", "stain_type"}),
)
