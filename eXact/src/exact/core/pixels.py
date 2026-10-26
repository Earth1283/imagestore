"""Canonical pixel-buffer conventions.

The authoritative image is a NumPy ``uint8`` array with shape ``(height, width, channels)``:

* C-contiguous, row-major: row 0 is the top row of the PNG, column 0 the left column.
* Channel order is ``R, G, B`` for ``"RGB"`` images and ``R, G, B, A`` for ``"RGBA"``.
* Alpha is *straight* (non-premultiplied), exactly as stored in the PNG.
* No colour management, gamma or profile has been applied — values are the raw
  8-bit samples from the PNG's image data.

The *canonical byte stream* of an image is ``buffer.tobytes()`` in that order, i.e. for
pixel ``(x, y)`` channel ``c`` the byte offset is ``(y * width + x) * channels + c``.
All pixel hashes in eXact are SHA-256 over exactly that byte stream (dimensions and mode
are reported separately and are *not* part of the hash).
"""

from __future__ import annotations

import hashlib

import numpy as np

SUPPORTED_MODES: dict[str, int] = {"RGB": 3, "RGBA": 4}
CHANNEL_NAMES: dict[str, tuple[str, ...]] = {
    "RGB": ("R", "G", "B"),
    "RGBA": ("R", "G", "B", "A"),
}


class BufferError_(ValueError):
    """Raised when an array does not follow the canonical buffer conventions."""


def channels_for_mode(mode: str) -> int:
    try:
        return SUPPORTED_MODES[mode]
    except KeyError:
        raise BufferError_(f"Unsupported mode {mode!r}; eXact v0.1.0 supports RGB and RGBA") from None


def mode_for_channels(channels: int) -> str:
    for mode, count in SUPPORTED_MODES.items():
        if count == channels:
            return mode
    raise BufferError_(f"No supported mode has {channels} channels")


def validate_buffer(arr: np.ndarray) -> str:
    """Check that *arr* is a canonical buffer and return its mode."""
    if not isinstance(arr, np.ndarray):
        raise BufferError_("pixel buffer must be a numpy array")
    if arr.dtype != np.uint8:
        raise BufferError_(f"pixel buffer must be uint8, got {arr.dtype}")
    if arr.ndim != 3:
        raise BufferError_(f"pixel buffer must be (height, width, channels), got shape {arr.shape}")
    if not arr.flags.c_contiguous:
        raise BufferError_("pixel buffer must be C-contiguous")
    if arr.shape[0] < 1 or arr.shape[1] < 1:
        raise BufferError_("pixel buffer must be at least 1x1")
    return mode_for_channels(arr.shape[2])


def buffer_sha256(arr: np.ndarray) -> str:
    """SHA-256 of the canonical channel bytes (no copy for contiguous buffers)."""
    validate_buffer(arr)
    return hashlib.sha256(memoryview(arr).cast("B")).hexdigest()


def pixel_offset(x: int, y: int, width: int, channels: int) -> int:
    """Byte offset of pixel (x, y) in the canonical byte stream."""
    return (y * width + x) * channels


def pixel_changes(values: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Bool mask of pixels whose channels differ (the last axis is the channel axis).

    RGBA compares each pixel as one uint32, RGB ORs the channels: both are many times
    faster than ``(values != reference).any(axis=-1)``, whose inner loop is 3 or 4 long.
    """
    if values.shape[-1] == 4 and values.strides[-1] == reference.strides[-1] == 1:
        try:
            return (values.view(np.uint32) != reference.view(np.uint32))[..., 0]
        except ValueError:  # a view whose pixels are not 4 contiguous bytes
            pass
    neq = values[..., 0] != reference[..., 0]
    for c in range(1, values.shape[-1]):
        neq |= values[..., c] != reference[..., c]
    return neq


def diff_counts(values: np.ndarray, reference: np.ndarray) -> tuple[int, int]:
    """Return (differing pixels, differing channel bytes) between two equal-shape arrays.

    The last axis is the channel axis.
    """
    return int(np.count_nonzero(pixel_changes(values, reference))), int(np.count_nonzero(values != reference))
