from __future__ import annotations

import numpy as np
import pytest
import tifffile


def _write_pyramid_tiff(path, *, base=1024, tile=128, levels=4):
    """A small tile-aligned pyramidal TIFF that OpenSlide recognizes as
    'generic-tiff'. Dimensions are chosen as exact multiples of the tile
    size at every level so there's no incidental edge-tile zero padding —
    keeps zero-tail assertions in tests unambiguous.
    """
    arrays = []
    current = (np.random.default_rng(0).random((base, base, 3)) * 255).astype("uint8")
    for _ in range(levels):
        arrays.append(current)
        current = current[::2, ::2]

    with tifffile.TiffWriter(path, bigtiff=False) as tif:
        for i, arr in enumerate(arrays):
            tif.write(arr, tile=(tile, tile), photometric="rgb", subfiletype=0 if i == 0 else 1)
    return path


@pytest.fixture
def clean_slide_path(tmp_path):
    return _write_pyramid_tiff(tmp_path / "clean.svs")


@pytest.fixture
def truncated_slide_path(tmp_path, clean_slide_path):
    """A copy of the clean fixture with its last 30% zero-filled, matching
    the observed real-world truncated-upload failure mode.
    """
    data = bytearray(clean_slide_path.read_bytes())
    size = len(data)
    cut = int(size * 0.70)
    corrupted = bytes(data[:cut]) + bytes(size - cut)
    out = tmp_path / "truncated.svs"
    out.write_bytes(corrupted)
    return out


@pytest.fixture
def bare_truncated_slide_path(tmp_path, clean_slide_path):
    """A copy of the clean fixture simply cut short — no zero-padding back
    to the original size. The zero-tail heuristic can't see this (there's no
    zero run, just an abrupt end), which is exactly what
    check_structural_completeness exists to catch instead.
    """
    data = clean_slide_path.read_bytes()
    cut = int(len(data) * 0.70)
    out = tmp_path / "bare_truncated.svs"
    out.write_bytes(data[:cut])
    return out
