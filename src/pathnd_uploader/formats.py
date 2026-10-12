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
from typing import Protocol

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


class BundleFS(Protocol):
    """Minimal read-only view of the directory an index file lives in, so the
    same bundle checks run against a local disk or a GCS prefix. All paths are
    relative to the index file's parent directory, '/'-separated.
    """

    def is_file(self, rel: str) -> bool: ...
    def is_dir(self, rel: str) -> bool: ...
    def read_text(self, rel: str) -> str: ...


class BundleUnreadable(Exception):
    """A bundle file exists but can't be read (local I/O error). Network errors
    from remote backends are deliberately NOT wrapped in this — they must
    propagate so callers can classify them as transient, not as a bad file.
    """


class LocalBundleFS:
    def __init__(self, base: Path) -> None:
        self._base = base

    def is_file(self, rel: str) -> bool:
        return (self._base / rel).is_file()

    def is_dir(self, rel: str) -> bool:
        return (self._base / rel).is_dir()

    def read_text(self, rel: str) -> str:
        try:
            return (self._base / rel).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise BundleUnreadable(str(exc)) from exc


def _parse_ini(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-significant
    parser.read_string(text.lstrip("\ufeff"))
    return parser


def _safe_rel(name: str) -> bool:
    """Index files are untrusted input: absolute or `..` references must not
    be probed (or treated as satisfying the bundle).
    """
    normalized = name.replace("\\", "/")
    return bool(name) and not Path(name).is_absolute() and not normalized.startswith("/") and ".." not in normalized.split("/")


def _mrxs_missing(fs: BundleFS, index_name: str) -> list[str]:
    stem = Path(index_name).stem
    if not fs.is_dir(stem):
        return [f"data directory '{stem}/'"]
    slidedat = f"{stem}/Slidedat.ini"
    if not fs.is_file(slidedat):
        return [f"'{slidedat}'"]
    if not fs.is_file(f"{stem}/Index.dat"):
        return [f"'{stem}/Index.dat'"]
    try:
        ini = _parse_ini(fs.read_text(slidedat))
    except (configparser.Error, BundleUnreadable) as exc:
        return [f"a readable '{slidedat}' ({exc.__class__.__name__})"]
    entries = (
        [v.strip() for k, v in ini.items("DATAFILE") if re.fullmatch(r"FILE_\d+", k)] if ini.has_section("DATAFILE") else []
    )
    if not entries:
        return [f"data file entries in '{slidedat}' (no [DATAFILE] FILE_n keys)"]
    missing = []
    for name in entries:
        if not _safe_rel(name):
            missing.append(f"a valid data file reference ({name!r} is empty or escapes the data directory)")
        elif not fs.is_file(f"{stem}/{name}"):
            missing.append(f"'{stem}/{name}'")
    declared = ini.get("DATAFILE", "FILE_COUNT", fallback="").strip()
    if declared.isdigit() and int(declared) != len(entries):
        missing.append(f"data file entries (FILE_COUNT={declared}, listed {len(entries)})")
    return missing


_VMS_REFERENCE_SUFFIXES = {".jpg", ".jpeg", ".opt", ".ngr", ".map"}
_VMS_REF_KEY = re.compile(r"^(ImageFile|MapFile|OptimisationFile|MacroImage|NGRImageFile)", re.IGNORECASE)


def _vms_missing(fs: BundleFS, index_name: str) -> list[str]:
    """.vms/.vmu are INI index files referencing sibling image/optimisation
    files by name; any referenced file that's absent makes the slide unreadable.
    """
    try:
        ini = _parse_ini(fs.read_text(index_name))
    except (configparser.Error, BundleUnreadable) as exc:
        return [f"a readable index file ({exc.__class__.__name__})"]
    missing = []
    for section in ini.sections():
        for key, value in ini.items(section):
            name = value.strip()
            if _VMS_REF_KEY.match(key) and Path(name.replace("\\", "/")).suffix.lower() in _VMS_REFERENCE_SUFFIXES:
                if not _safe_rel(name) or not fs.is_file(name):
                    missing.append(f"'{name}'")
    return missing


def bundle_missing_parts(path: Path | str, fs: BundleFS | None = None) -> list[str]:
    """Human-readable descriptions of sidecar files a bundle format is missing.
    Empty for non-bundle formats or a complete bundle. `path` is the index
    file; `fs` defaults to the local directory it sits in.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in (".mrxs", ".vms", ".vmu"):
        return []
    fs = fs or LocalBundleFS(path.parent)
    check = _mrxs_missing if ext == ".mrxs" else _vms_missing
    return check(fs, path.name)
