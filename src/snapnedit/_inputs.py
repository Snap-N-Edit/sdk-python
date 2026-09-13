"""Turning whatever a caller passed as an image into `(bytes, mime)`."""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import IO, Any, Union

from .errors import ErrorCode, SnapneditError
from .models import UrlInput

__all__ = ["ImageInput", "read_image", "sniff_mime"]

ImageInput = Union[bytes, bytearray, memoryview, "PathLike[str]", str, IO[bytes]]
"""Raw bytes, a filesystem path (`str` or `Path`), or any binary file object."""

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_mime(data: bytes) -> str | None:
    """Identify an image from its magic bytes, or return `None`."""
    for prefix, mime in _MAGIC:
        if data.startswith(prefix):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _mime_for_name(name: str) -> str | None:
    """Guess a mime from a filename."""
    guessed, _ = mimetypes.guess_type(name)
    return guessed


def read_image(value: ImageInput, mime: str | None = None) -> tuple[bytes, str]:
    """Read `value` into bytes and settle on a mime type.

    Accepts raw bytes, a `Path` (or path string), or an open binary file
    object. The mime is the caller's if given, otherwise sniffed from the
    bytes, otherwise guessed from the filename, otherwise
    `application/octet-stream` (which the api will refuse — pass `mime=`).
    """
    name: str | None = None
    data: bytes
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    elif isinstance(value, (str, PathLike)):
        path = Path(value)
        data = path.read_bytes()
        name = path.name
    elif hasattr(value, "read"):
        chunk = value.read()
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise SnapneditError(
                ErrorCode.INVALID_INPUT,
                "file object must be opened in binary mode ('rb')",
            )
        data = bytes(chunk)
        raw_name = getattr(value, "name", None)
        name = raw_name if isinstance(raw_name, str) else None
    else:  # pragma: no cover - defensive
        raise SnapneditError(
            ErrorCode.INVALID_INPUT, f"unsupported image input: {type(value).__name__}"
        )

    if not data:
        raise SnapneditError(ErrorCode.INVALID_INPUT, "image input is empty")

    resolved = mime or sniff_mime(data) or (_mime_for_name(name) if name else None)
    return data, resolved or "application/octet-stream"


def is_url_input(value: Any) -> bool:
    """Return whether `value` is the `{ url }` form of a job input."""
    if isinstance(value, UrlInput):
        return True
    return isinstance(value, Mapping) and isinstance(value.get("url"), str)


def as_url_input(value: Any) -> UrlInput | None:
    """Normalize a url input, or return `None` when `value` is not one."""
    if isinstance(value, UrlInput):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("url"), str):
        return UrlInput(url=str(value["url"]))
    return None
