"""Streaming CRC32C computation — the same algorithm GCS uses for its own
object integrity metadata, so a value computed here can be compared directly
against `blob.crc32c` without re-deriving it.
"""

from __future__ import annotations

import base64
from pathlib import Path

import google_crc32c

_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB


def compute_crc32c_base64(path: Path) -> str:
    checksum = google_crc32c.Checksum()
    with Path(path).open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            checksum.update(chunk)
    return base64.b64encode(checksum.digest()).decode("ascii")
