"""Lossless geometric transforms (the dihedral group D4).

Every transform is a pure index permutation: each output pixel is a byte-for-byte copy
of exactly one input pixel, with all channels moved together. No interpolation,
resampling or value arithmetic is ever performed.

Note: a transform preserves every pixel *value* but changes pixel *coordinates*, so the
canonical byte stream (and its SHA-256) changes. Data stored in pixel order (e.g. an
ImageStore payload) is only decodable again after applying the inverse transform.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

ROTATE_CW = "rotate_cw"
ROTATE_CCW = "rotate_ccw"
ROTATE_180 = "rotate_180"
FLIP_H = "flip_h"
FLIP_V = "flip_v"

OPS = (ROTATE_CW, ROTATE_CCW, ROTATE_180, FLIP_H, FLIP_V)

LABELS = {
    ROTATE_CW: "Rotate 90° Clockwise",
    ROTATE_CCW: "Rotate 90° Counterclockwise",
    ROTATE_180: "Rotate 180°",
    FLIP_H: "Flip Horizontal",
    FLIP_V: "Flip Vertical",
}

INVERSE = {
    ROTATE_CW: ROTATE_CCW,
    ROTATE_CCW: ROTATE_CW,
    ROTATE_180: ROTATE_180,
    FLIP_H: FLIP_H,
    FLIP_V: FLIP_V,
}


def _check_op(op: str) -> None:
    if op not in INVERSE:
        raise ValueError(f"unknown transform {op!r}")


def transform_view(arr: np.ndarray, op: str) -> np.ndarray:
    """Return a (non-contiguous) view of *arr* with *op* applied. Never copies."""
    _check_op(op)
    if op == ROTATE_CW:
        return np.rot90(arr, k=-1, axes=(0, 1))
    if op == ROTATE_CCW:
        return np.rot90(arr, k=1, axes=(0, 1))
    if op == ROTATE_180:
        return arr[::-1, ::-1]
    if op == FLIP_H:
        return arr[:, ::-1]
    return arr[::-1]


def apply_transform(arr: np.ndarray, op: str) -> np.ndarray:
    """Apply *op* and return a new C-contiguous canonical buffer (one full copy)."""
    return np.ascontiguousarray(transform_view(arr, op))


def transposes(op: str) -> bool:
    _check_op(op)
    return op in (ROTATE_CW, ROTATE_CCW)


@dataclass(frozen=True)
class Orientation:
    """An element of D4, mapping the original image to the current geometry.

    Represented as ``T(x) = rotate_cw^k(flip_h^flipped(x))``.
    """

    k: int = 0
    flipped: bool = False

    def then(self, op: str) -> "Orientation":
        """The orientation after additionally applying *op*."""
        _check_op(op)
        k, f = self.k, self.flipped
        if op == ROTATE_CW:
            return Orientation((k + 1) % 4, f)
        if op == ROTATE_CCW:
            return Orientation((k + 3) % 4, f)
        if op == ROTATE_180:
            return Orientation((k + 2) % 4, f)
        # flip_h ∘ rot^k = rot^-k ∘ flip_h ; flip_v = rot^2 ∘ flip_h
        if op == FLIP_H:
            return Orientation((-k) % 4, not f)
        return Orientation((2 - k) % 4, not f)

    def view(self, arr: np.ndarray) -> np.ndarray:
        """View of *arr* (in original geometry) mapped into this orientation. Never copies."""
        v = arr[:, ::-1] if self.flipped else arr
        return np.rot90(v, k=-self.k, axes=(0, 1)) if self.k else v

    @property
    def is_identity(self) -> bool:
        return self.k == 0 and not self.flipped

    @property
    def transposes(self) -> bool:
        return self.k % 2 == 1

    def describe(self) -> str:
        names = {
            (0, False): "Identity (original orientation)",
            (1, False): "Rotated 90° clockwise",
            (2, False): "Rotated 180°",
            (3, False): "Rotated 90° counterclockwise",
            (0, True): "Flipped horizontally",
            (2, True): "Flipped vertically",
            (1, True): "Transposed across anti-diagonal (flip H, then rotate 90° CW)",
            (3, True): "Transposed across main diagonal (flip H, then rotate 90° CCW)",
        }
        return names[(self.k, self.flipped)]
