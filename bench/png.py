"""Minimal pure-Python PNG writer (zlib + struct) for 8-bit grayscale or BGR numpy arrays.

No OpenCV. BGR input (``H x W x 3``) is written as RGB; grayscale (``H x W``) as 8-bit gray.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

__all__ = ["PNG_SIGNATURE", "encode_png", "write_png"]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_png(image: np.ndarray) -> bytes:
    """Encode a ``uint8`` ``H x W`` (gray) or ``H x W x 3`` (BGR) array as PNG bytes."""
    img = np.asarray(image)
    if img.dtype != np.uint8:
        raise ValueError(f"expected a uint8 image, got {img.dtype}")
    if img.ndim == 2:
        color_type = 0
    elif img.ndim == 3 and img.shape[2] == 3:
        color_type = 2
        img = img[:, :, ::-1]  # BGR -> RGB
    else:
        raise ValueError(f"expected H x W or H x W x 3 image, got shape {img.shape}")
    height, width = img.shape[:2]
    if height == 0 or width == 0:
        raise ValueError("image is empty")
    rows = np.ascontiguousarray(img).reshape(height, -1)
    raw = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rows], axis=1)  # filter type 0
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
        + _chunk(b"IEND", b"")
    )


def write_png(path: Path | str, image: np.ndarray) -> None:
    """Write ``image`` to ``path`` as PNG, creating parent directories."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(encode_png(image))
