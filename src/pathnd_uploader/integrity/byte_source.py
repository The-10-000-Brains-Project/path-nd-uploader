"""Uniform byte-range access over a local file or a remote GCS object.

The cheap integrity checks (zero-tail, header magic) only ever need a size
and a handful of byte ranges, never the whole file. Routing both local
pre-upload validation and remote bucket auditing through this same interface
means those checks are written once and run unmodified in both places —
critically, auditing an existing multi-GB slide in a bucket does *not*
require downloading it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ByteRangeSource(Protocol):
    def size(self) -> int: ...

    def read_range(self, start: int, length: int) -> bytes:
        """Reads `length` bytes starting at byte offset `start`."""
        ...

    def describe(self) -> str:
        """A human-readable location string (local path or gs:// URI)."""
        ...


class LocalFileSource:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def size(self) -> int:
        return self._path.stat().st_size

    def read_range(self, start: int, length: int) -> bytes:
        with self._path.open("rb") as f:
            f.seek(start)
            return f.read(length)

    def describe(self) -> str:
        return str(self._path)

    @property
    def path(self) -> Path:
        return self._path


class GCSBlobSource:
    """Wraps a google.cloud.storage.Blob for range-read access.

    `blob` must already exist server-side (i.e. this wraps a blob returned
    by `bucket.list_blobs()` or `bucket.get_blob()`, not a freshly
    constructed local handle).
    """

    def __init__(self, blob) -> None:
        self._blob = blob
        if self._blob.size is None:
            self._blob.reload()

    def size(self) -> int:
        return self._blob.size

    def read_range(self, start: int, length: int) -> bytes:
        end = start + length - 1  # GCS range downloads use inclusive end offsets
        return self._blob.download_as_bytes(start=start, end=end)

    def describe(self) -> str:
        return f"gs://{self._blob.bucket.name}/{self._blob.name}"

    @property
    def blob(self):
        return self._blob
