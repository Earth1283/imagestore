"""Display transformations — viewport-sized, throwaway, never written back.

Each function takes a small region (already sliced from the authoritative buffer for the
visible viewport) and returns a new opaque ``(h, w, 3)`` uint8 RGB array for the screen.
Qt only ever receives these opaque display arrays, so premultiplied-alpha formats inside
Qt can never touch real pixel data.
"""

from __future__ import annotations

import numpy as np

DISPLAY_MODES = {
    "composite": "Composite over checkerboard",
    "rgb": "RGB only (ignore alpha)",
    "alpha": "Alpha channel (grayscale)",
    "r": "Red channel",
    "g": "Green channel",
    "b": "Blue channel",
}
_CHANNEL_INDEX = {"r": 0, "g": 1, "b": 2, "alpha": 3}
CHECKER_LIGHT = 0xCC
CHECKER_DARK = 0x99
HIGHLIGHT = np.array([255, 0, 255], dtype=np.uint16)


def checker_cell(zoom: float) -> int:
    """Checkerboard cell size in image pixels (about 8 screen pixels, at least 1 pixel)."""
    return max(1, int(8 / zoom))


def to_display_rgb(region: np.ndarray, mode: str, display_mode: str, x0: int, y0: int, step: int,
                   zoom: float) -> np.ndarray:
    h, w = region.shape[:2]
    if display_mode in _CHANNEL_INDEX:
        idx = _CHANNEL_INDEX[display_mode]
        if idx >= region.shape[2]:  # alpha view of an RGB image: fully opaque
            gray = np.full((h, w), 255, dtype=np.uint8)
        else:
            gray = region[..., idx]
        return np.ascontiguousarray(np.repeat(gray[..., None], 3, axis=2))
    rgb = region[..., :3]
    if mode != "RGBA" or display_mode == "rgb":
        return np.ascontiguousarray(rgb)
    cell = checker_cell(zoom)
    ys = np.arange(y0, y0 + h * step, step)[:h] // cell
    xs = np.arange(x0, x0 + w * step, step)[:w] // cell
    bg = np.where((ys[:, None] + xs[None, :]) & 1, CHECKER_DARK, CHECKER_LIGHT).astype(np.uint16)
    a = region[..., 3].astype(np.uint16)
    inv = 255 - a
    out = (rgb.astype(np.uint16) * a[..., None] + (bg * inv)[..., None] + 127) // 255
    return out.astype(np.uint8)


def tint_changed(rgb: np.ndarray, changed: np.ndarray) -> None:
    """In-place magenta tint of *changed* pixels in a display array."""
    if changed.any():
        rgb[changed] = ((rgb[changed].astype(np.uint16) + HIGHLIGHT) // 2).astype(np.uint8)


def heat_colors(heat: np.ndarray) -> np.ndarray:
    """Map per-pixel max channel difference to colour: unchanged = dark grey,
    any change = red (difference 1) through yellow (difference 255)."""
    out = np.empty(heat.shape + (3,), dtype=np.uint8)
    changed = heat > 0
    out[...] = 28
    out[changed, 0] = 255
    out[changed, 1] = heat[changed]
    out[changed, 2] = 0
    return out


def max_pool(arr: np.ndarray, step: int) -> np.ndarray:
    """Downsample a 2-D array by taking the maximum of each step×step block, so that a
    single changed pixel stays visible when zoomed out."""
    if step == 1:
        return arr
    h, w = arr.shape
    hh, ww = -(-h // step), -(-w // step)
    padded = np.zeros((hh * step, ww * step), dtype=arr.dtype)
    padded[:h, :w] = arr
    return padded.reshape(hh, step, ww, step).max(axis=(1, 3))
