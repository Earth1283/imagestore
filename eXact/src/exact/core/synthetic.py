"""Deterministic synthetic images that look like binary payloads (ImageStore-style).

Used by the tests, the benchmark and ``exact-cli make-test-image``.
"""

from __future__ import annotations

import hashlib
import struct

import numpy as np

MAGIC = b"IMGSTORE"


def payload_image(width: int, height: int, mode: str = "RGBA", seed: int = 921) -> np.ndarray:
    """Pseudo-random bytes in every channel, plus deliberate edge cases.

    * A header in the first pixels: magic, dimensions, SHA-256 of the rest — like a
      payload container would store.
    * A band of fully transparent pixels (alpha 0) with non-zero RGB.
    * Pixels with alpha 255 and alpha 1, and channels at 0x00 / 0xFF.
    """
    channels = 4 if mode == "RGBA" else 3
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(height, width, channels), dtype=np.uint8)
    flat = arr.reshape(-1)
    if mode == "RGBA" and height >= 4:
        band = arr[height // 3: height // 3 + max(1, height // 16)]
        band[..., 3] = 0
        band[..., 0] |= 0x01  # make sure the hidden RGB is non-zero
        arr[-1, :, 3] = 255
        arr[-2, :, 3] = 1
    if width >= 2:
        arr[0, -1] = 0
        arr[-1, 0] = 255
    body_hash = hashlib.sha256(flat[64:].tobytes()).digest()
    header = MAGIC + struct.pack(">II", width, height) + body_hash
    if flat.size >= 64:
        flat[: len(header)] = np.frombuffer(header, dtype=np.uint8)
    return arr
