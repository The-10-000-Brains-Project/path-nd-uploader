"""WSI file format recognition and multi-file bundle handling."""

from __future__ import annotations

from pathlib import Path

SUPPORTED_EXTENSIONS = {
    ".svs",  # Aperio
    ".ndpi",  # Hamamatsu
    ".scn",  # Leica
    ".tif",
    ".tiff",  # generic / OME-TIFF
    ".mrxs",  # 3DHistech MIRAX (multi-file bundle)
    ".vms",
    ".vmu",  # Hamamatsu (older)
    ".bif",  # Ventana
}


class UnsupportedSlideFormat(ValueError):
    pass


def validate_extension(path: Path) -> None:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise UnsupportedSlideFormat(
            f"'{path.suffix}' is not a supported WSI format (supported: {sorted(SUPPORTED_EXTENSIONS)})"
        )


def is_mrxs_bundle(path: Path) -> bool:
    return path.suffix.lower() == ".mrxs"


def mrxs_bundle_files(mrxs_path: Path) -> list[Path]:
    """A .mrxs slide is an index file plus a sibling directory of the same
    stem containing the actual tile data. Both must exist and be uploaded
    together as one logical unit.
    """
    data_dir = mrxs_path.with_suffix("")
    if not data_dir.is_dir():
        raise FileNotFoundError(
            f"'{mrxs_path.name}' is a MIRAX slide but its data directory '{data_dir}' was not found"
        )
    return [mrxs_path, *sorted(p for p in data_dir.rglob("*") if p.is_file())]
