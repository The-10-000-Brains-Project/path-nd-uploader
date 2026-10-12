"""WSI file format registry, recognition, and multi-file bundle handling.

Every format here is one OpenSlide can open, so the deep check works for
all of them. Formats OpenSlide can't read (e.g. Zeiss .czi, Philips
.isyntax, DICOM WSI) are rejected as unsupported rather than half-validated.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path

TIFF_MAGICS = frozenset(
    {
        b"II*\x00",  # little-endian classic TIFF
        b"MM\x00*",  # big-endian classic TIFF
        b"II+\x00",  # little-endian BigTIFF
        b"MM\x00+",  # big-endian BigTIFF
    }
)
SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(frozen=True)
class SlideFormat:
    name: str
    extensions: tuple[str, ...]
    tiff_based: bool = False
    # Valid leading bytes for the format's header check; None = no check.
    magics: frozenset[bytes] | None = None
    # Multi-file formats whose sidecar files must all be present.
    bundle: bool = False


FORMATS: tuple[SlideFormat, ...] = (
    SlideFormat("Aperio", (".svs",), tiff_based=True, magics=TIFF_MAGICS),
    SlideFormat("Hamamatsu NDPI", (".ndpi",), tiff_based=True, magics=TIFF_MAGICS),
    SlideFormat("Leica", (".scn",), tiff_based=True, magics=TIFF_MAGICS),
    SlideFormat("Ventana", (".bif",), tiff_based=True, magics=TIFF_MAGICS),
    SlideFormat("Generic / OME / Philips TIFF", (".tif", ".tiff"), tiff_based=True, magics=TIFF_MAGICS),
    SlideFormat("3DHistech MIRAX", (".mrxs",), bundle=True),
    SlideFormat("Hamamatsu VMS/VMU", (".vms", ".vmu"), bundle=True),
    SlideFormat("Sakura", (".svslide",), magics=frozenset({SQLITE_MAGIC})),
)

_BY_EXTENSION = {ext: fmt for fmt in FORMATS for ext in fmt.extensions}

SUPPORTED_EXTENSIONS = set(_BY_EXTENSION)
TIFF_BASED_EXTENSIONS = {ext for fmt in FORMATS if fmt.tiff_based for ext in fmt.extensions}

# Real pathology formats we recognize but can't validate; named in the error
# so users know it's a known gap rather than a typo.
KNOWN_UNSUPPORTED_EXTENSIONS = {
    ".czi": "Zeiss",
    ".vsi": "Olympus",
    ".qptiff": "Akoya/PerkinElmer",
    ".isyntax": "Philips iSyntax",
    ".dcm": "DICOM WSI",
    ".zarr": "Zarr",
    ".jp2": "JPEG 2000",
    ".dzi": "Deep Zoom",
}


class UnsupportedSlideFormat(ValueError):
    pass


def get_format(path_or_ext: Path | str) -> SlideFormat | None:
    ext = path_or_ext.suffix if isinstance(path_or_ext, Path) else str(path_or_ext)
    return _BY_EXTENSION.get(ext.lower())


def validate_extension(path: Path) -> None:
    ext = path.suffix.lower()
    if ext in _BY_EXTENSION:
        return
    known = KNOWN_UNSUPPORTED_EXTENSIONS.get(ext)
    prefix = f"'{path.suffix}' ({known}) is a recognized pathology format but not yet supported" if known else (
        f"'{path.suffix}' is not a supported WSI format"
    )
    raise UnsupportedSlideFormat(f"{prefix} (supported: {sorted(SUPPORTED_EXTENSIONS)})")


def is_mrxs_bundle(path: Path) -> bool:
    return path.suffix.lower() == ".mrxs"


def is_bundle(path: Path) -> bool:
    fmt = get_format(path)
    return fmt is not None and fmt.bundle


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


def _read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-significant
    parser.read_string(path.read_text(encoding="utf-8-sig", errors="replace"))
    return parser


def _mrxs_missing(mrxs_path: Path) -> list[str]:
    data_dir = mrxs_path.with_suffix("")
    if not data_dir.is_dir():
        return [f"data directory '{data_dir.name}/'"]
    slidedat = data_dir / "Slidedat.ini"
    if not slidedat.is_file():
        return [f"'{data_dir.name}/Slidedat.ini'"]
    try:
        ini = _read_ini(slidedat)
    except configparser.Error as exc:
        return [f"a parseable '{data_dir.name}/Slidedat.ini' ({exc.__class__.__name__})"]
    if not ini.has_section("DATAFILE"):
        return []
    return [
        f"'{data_dir.name}/{name}'"
        for key, name in ini.items("DATAFILE")
        if re.fullmatch(r"FILE_\d+", key) and not (data_dir / name).is_file()
    ]


_VMS_REFERENCE_SUFFIXES = {".jpg", ".jpeg", ".opt", ".ngr", ".map"}
_VMS_REF_KEY = re.compile(r"^(ImageFile|MapFile|OptimisationFile|MacroImage|NGRImageFile)", re.IGNORECASE)


def _vms_missing(index_path: Path) -> list[str]:
    """.vms/.vmu are INI index files referencing sibling image/optimisation
    files by name; any referenced file that's absent makes the slide unreadable.
    """
    try:
        ini = _read_ini(index_path)
    except configparser.Error as exc:
        return [f"a parseable index file ({exc.__class__.__name__})"]
    missing = []
    for section in ini.sections():
        for key, value in ini.items(section):
            name = value.strip()
            if _VMS_REF_KEY.match(key) and Path(name).suffix.lower() in _VMS_REFERENCE_SUFFIXES:
                if not (index_path.parent / name).is_file():
                    missing.append(f"'{name}'")
    return missing


def bundle_missing_parts(path: Path) -> list[str]:
    """Human-readable descriptions of sidecar files a bundle format is missing.
    Empty for non-bundle formats or a complete bundle. Local filesystem only.
    """
    ext = path.suffix.lower()
    if ext == ".mrxs":
        return _mrxs_missing(path)
    if ext in (".vms", ".vmu"):
        return _vms_missing(path)
    return []
