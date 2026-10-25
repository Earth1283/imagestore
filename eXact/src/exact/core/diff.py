"""Raw pixel-buffer comparison.

Comparisons always operate on decoded canonical channel bytes — never on rendered or
composited output. Three different notions of "the same" are reported separately:

1. Byte-identical PNG files      (SHA-256 of the file bytes)
2. Identical decoded pixel data  (SHA-256 of canonical channel bytes)
3. Visually identical            (identical after compositing over *any* background;
                                  differs from 2 when only the RGB of fully transparent
                                  pixels changed — exactly what lossy editors tend to do)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .pixels import buffer_sha256, validate_buffer
from .png_chunks import file_sha256
from .png_io import load_png

STRIP_ROWS = 256


class ComparisonMismatch(ValueError):
    """The two images cannot be compared pixel-for-pixel (dimensions or mode differ)."""


@dataclass
class DiffResult:
    width: int
    height: int
    mode: str
    changed_pixels: int
    changed_bytes: int
    first_diff: tuple[int, int] | None  # (x, y) in row-major scan order
    first_values_a: tuple[int, ...] | None
    first_values_b: tuple[int, ...] | None
    bbox: tuple[int, int, int, int] | None  # (x0, y0, x1, y1) inclusive
    invisible_pixels: int  # changes confined to RGB of pixels that are fully transparent in both
    sha256_a: str
    sha256_b: str
    heatmap: np.ndarray | None  # (H, W) uint8: max absolute channel difference per pixel

    @property
    def total_pixels(self) -> int:
        return self.width * self.height

    @property
    def total_bytes(self) -> int:
        return self.total_pixels * (4 if self.mode == "RGBA" else 3)

    @property
    def identical(self) -> bool:
        return self.changed_bytes == 0

    @property
    def match_percent(self) -> float:
        return 100.0 * (self.total_pixels - self.changed_pixels) / self.total_pixels

    @property
    def visually_identical(self) -> bool:
        return self.changed_pixels == self.invisible_pixels


def compare_buffers(a: np.ndarray, b: np.ndarray, *, heatmap: bool = True) -> DiffResult:
    mode_a, mode_b = validate_buffer(a), validate_buffer(b)
    if mode_a != mode_b:
        raise ComparisonMismatch(f"modes differ: {mode_a} vs {mode_b}")
    if a.shape != b.shape:
        raise ComparisonMismatch(
            f"dimensions differ: {a.shape[1]}×{a.shape[0]} vs {b.shape[1]}×{b.shape[0]}")
    height, width = a.shape[:2]
    heat = np.zeros((height, width), dtype=np.uint8) if heatmap else None
    changed_pixels = changed_bytes = invisible = 0
    first = None
    row_any = np.zeros(height, dtype=bool)
    col_any = np.zeros(width, dtype=bool)
    for y0 in range(0, height, STRIP_ROWS):
        y1 = min(height, y0 + STRIP_ROWS)
        sa, sb = a[y0:y1], b[y0:y1]
        neq = sa != sb
        pix = neq.any(axis=2)
        n = int(pix.sum())
        if n == 0:
            continue
        changed_pixels += n
        changed_bytes += int(neq.sum())
        if first is None:
            idx = int(np.argmax(pix.ravel()))
            first = (idx % width, y0 + idx // width)
        row_any[y0:y1] |= pix.any(axis=1)
        col_any |= pix.any(axis=0)
        if mode_a == "RGBA":
            invisible += int((pix & (sa[..., 3] == 0) & (sb[..., 3] == 0)).sum())
        if heat is not None:
            heat[y0:y1] = np.abs(sa.astype(np.int16) - sb.astype(np.int16)).max(axis=2).astype(np.uint8)
    bbox = None
    if first is not None:
        rows, cols = np.nonzero(row_any)[0], np.nonzero(col_any)[0]
        bbox = (int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1]))
    return DiffResult(
        width=width, height=height, mode=mode_a,
        changed_pixels=changed_pixels, changed_bytes=changed_bytes,
        first_diff=first,
        first_values_a=tuple(int(v) for v in a[first[1], first[0]]) if first else None,
        first_values_b=tuple(int(v) for v in b[first[1], first[0]]) if first else None,
        bbox=bbox, invisible_pixels=invisible,
        sha256_a=buffer_sha256(a), sha256_b=buffer_sha256(b),
        heatmap=heat,
    )


@dataclass
class FileComparison:
    path_a: str
    path_b: str
    file_sha256_a: str
    file_sha256_b: str
    diff: DiffResult | None
    mismatch: str | None = None

    @property
    def files_identical(self) -> bool:
        return self.file_sha256_a == self.file_sha256_b

    def report_lines(self) -> list[str]:
        lines = [
            f"A: {self.path_a}",
            f"B: {self.path_b}",
            f"PNG files byte-identical:      {'YES' if self.files_identical else 'no'}",
        ]
        if self.mismatch:
            lines.append(f"Pixel comparison impossible:   {self.mismatch}")
            return lines
        lines += describe_diff(self.diff)
        return lines


def describe_diff(d: DiffResult) -> list[str]:
    lines = [
        f"Decoded pixel buffers identical: {'YES' if d.identical else 'no'}",
        f"Visually identical (composited): {'YES' if d.visually_identical else 'no'}",
        f"Pixel SHA-256 A: {d.sha256_a}",
        f"Pixel SHA-256 B: {d.sha256_b}",
        f"Dimensions / mode: {d.width}×{d.height} {d.mode}",
        f"Exact pixel match: {d.match_percent:.6f}% ({d.total_pixels - d.changed_pixels} of {d.total_pixels})",
        f"Changed pixels: {d.changed_pixels}",
        f"Changed channel bytes: {d.changed_bytes} of {d.total_bytes}",
    ]
    if d.first_diff:
        x, y = d.first_diff
        lines.append(f"First differing pixel: ({x}, {y})  A={d.first_values_a}  B={d.first_values_b}")
        x0, y0, x1, y1 = d.bbox
        lines.append(f"Changed-region bounding box: x={x0}..{x1}, y={y0}..{y1} ({x1 - x0 + 1}×{y1 - y0 + 1})")
    if d.mode == "RGBA" and d.invisible_pixels:
        lines.append(
            f"Invisible changes: {d.invisible_pixels} pixel(s) differ only in the RGB of fully "
            "transparent pixels (looks identical, bytes are not)")
    return lines


def compare_files(path_a: str, path_b: str, *, heatmap: bool = True) -> FileComparison:
    a = load_png(path_a)
    b = load_png(path_b)
    result = FileComparison(a.path, b.path, a.layout.file_sha256, b.layout.file_sha256, None)
    try:
        result.diff = compare_buffers(a.pixels, b.pixels, heatmap=heatmap)
    except ComparisonMismatch as exc:
        result.mismatch = str(exc)
    return result


def file_identity(path: str) -> str:
    return file_sha256(path)
