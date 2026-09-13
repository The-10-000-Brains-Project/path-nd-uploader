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


class RangeReadFile:
    """Adapts any `ByteRangeSource` into a minimal seekable file-like object
    (read/seek/tell), so libraries that expect a file handle — e.g.
    `tifffile`, to parse a TIFF's directory structure — can be pointed at a
    remote GCS object and only fetch the small handful of bytes they
    actually need, not the whole file.
    """

    def __init__(self, source: ByteRangeSource) -> None:
        self._source = source
        self._size = source.size()
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        n = max(min(n, self._size - self._pos), 0)
        if n == 0:
            return b""
        data = self._source.read_range(self._pos, n)
        self._pos += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def close(self) -> None:
        pass


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
