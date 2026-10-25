"""The editable document: one authoritative pixel buffer plus provenance.

Invariants maintained by every operation:

* ``pixels`` is the only authoritative image data; it is a canonical buffer
  (see pixels.py) and is only mutated through methods of this class.
* Pixels are written by *replacement* only. eXact never blends, composites or
  premultiplies when editing; a channel write mask selects which channels are replaced.
* ``original`` is an immutable copy of the buffer as loaded. ``reference()`` is a view
  of it mapped through the accumulated geometric transform, so it always has the same
  shape as ``pixels``.
* ``content_diff_pixels`` / ``content_diff_bytes`` count pixels/bytes where
  ``pixels`` differs from ``reference()``. They are updated incrementally from each
  patch (geometric transforms permute both sides and leave the counts unchanged), so they
  are always exact without rescanning the image.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import transforms
from .history import Command, History, RectPatch, SparsePatch, TransformCommand
from .pixels import (
    CHANNEL_NAMES,
    buffer_sha256,
    channels_for_mode,
    diff_counts,
    validate_buffer,
)
from .png_chunks import ChunkLayout, file_sha256
from .png_io import (
    DEFAULT_COMPRESS_LEVEL,
    SavePlan,
    SaveReport,
    load_png,
    plan_save,
    save_png_verified,
)


class ChannelMismatchError(ValueError):
    """A paste would need to drop or invent channel data."""


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def x1(self) -> int:
        return self.x + self.w

    @property
    def y1(self) -> int:
        return self.y + self.h

    @property
    def empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    def clamp(self, width: int, height: int) -> "Rect":
        x0, y0 = max(0, self.x), max(0, self.y)
        x1, y1 = min(width, self.x1), min(height, self.y1)
        return Rect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    @classmethod
    def from_points(cls, ax: int, ay: int, bx: int, by: int) -> "Rect":
        x0, x1 = sorted((ax, bx))
        y0, y1 = sorted((ay, by))
        return cls(x0, y0, x1 - x0 + 1, y1 - y0 + 1)

    def __str__(self) -> str:
        return f"x={self.x}..{self.x1 - 1}, y={self.y}..{self.y1 - 1} ({self.w}×{self.h})"


@dataclass(frozen=True)
class OperationSummary:
    label: str
    changed_pixels: int
    changed_bytes: int
    bbox: Rect | None  # bounding box of changed pixels, None when nothing changed
    touched_pixels: int = 0  # pixels the operation wrote to (may have kept their value)
    note: str = ""

    def describe(self) -> str:
        if self.note:
            return f"{self.label}: {self.note}"
        where = f"; bounding box {self.bbox}" if self.bbox else ""
        touched = f" (of {self.touched_pixels} written)" if self.touched_pixels else ""
        return (
            f"{self.label}: {self.changed_pixels} pixel(s){touched} and "
            f"{self.changed_bytes} channel byte(s) changed{where}"
        )


@dataclass
class Clip:
    """Internal clipboard contents: a copy of raw channel values."""

    pixels: np.ndarray
    mode: str

    @property
    def width(self) -> int:
        return self.pixels.shape[1]

    @property
    def height(self) -> int:
        return self.pixels.shape[0]


def _bbox_of(ys: np.ndarray, xs: np.ndarray) -> Rect | None:
    if len(ys) == 0:
        return None
    return Rect.from_points(int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


def bresenham(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Integer line from (x0, y0) to (x1, y1) inclusive; no anti-aliasing."""
    points = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        points.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return points
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


class _Stroke:
    def __init__(self, color: np.ndarray, size: int, mask: np.ndarray, label: str):
        self.color = color
        self.size = size
        self.mask = mask
        self.label = label
        self.before: dict[int, np.ndarray] = {}
        self.last: tuple[int, int] | None = None


class Document:
    def __init__(
        self,
        pixels: np.ndarray,
        *,
        path: str | None = None,
        layout: ChunkLayout | None = None,
        warnings: list[str] | None = None,
        history_bytes: int = 1 << 30,
    ):
        self.mode = validate_buffer(pixels)
        self.pixels = pixels
        self.original = pixels.copy()
        self.original.setflags(write=False)
        self.original_sha256 = buffer_sha256(self.original)
        self.orientation = transforms.Orientation()
        self.history = History(history_bytes)
        self.path = path
        self.layout = layout
        self.warnings = list(warnings or [])
        self.content_diff_pixels = 0
        self.content_diff_bytes = 0
        self.generation = 0  # bumped on every mutation (for cache/worker invalidation)
        self.last_operation: OperationSummary | None = None
        self.saved_state = self.history.state_id
        self.saved_pixel_sha256 = self.original_sha256 if path else None
        self.disk_file_sha256 = layout.file_sha256 if layout else None
        self._stroke: _Stroke | None = None
        self._listeners: list = []
        self._encoded = None  # EncodedStream of the last file eXact wrote for this document
        self._rows_dirty: np.ndarray | None = None  # rows touched since that write; None = all


    @classmethod
    def open(cls, path: str, **kwargs) -> "Document":
        loaded = load_png(path)
        return cls(loaded.pixels, path=loaded.path, layout=loaded.layout, warnings=loaded.warnings, **kwargs)


    @property
    def width(self) -> int:
        return self.pixels.shape[1]

    @property
    def height(self) -> int:
        return self.pixels.shape[0]

    @property
    def channels(self) -> int:
        return self.pixels.shape[2]

    @property
    def channel_names(self) -> tuple[str, ...]:
        return CHANNEL_NAMES[self.mode]

    @property
    def dimensions_changed(self) -> bool:
        return self.pixels.shape != self.original.shape

    @property
    def dirty(self) -> bool:
        return self.history.state_id != self.saved_state

    @property
    def matches_original(self) -> bool:
        """True iff the current buffer is byte-identical to the buffer as loaded."""
        return self.orientation.is_identity and self.content_diff_pixels == 0

    def reference(self) -> np.ndarray:
        """The original buffer viewed in the current orientation (same shape as pixels)."""
        return self.orientation.view(self.original)

    def current_sha256(self) -> str:
        return buffer_sha256(self.pixels)

    def positional_diff(self) -> tuple[int, int] | None:
        """(pixels, bytes) differing from the original at the same (x, y) coordinates.

        None when the dimensions differ (positional comparison is undefined).
        """
        if self.dimensions_changed:
            return None
        if self.orientation.is_identity:
            return self.content_diff_pixels, self.content_diff_bytes
        return _strip_diff(self.pixels, self.original)

    def recount_content_diff(self) -> tuple[int, int]:
        """Full rescan of the content diff (used to cross-check the incremental counters)."""
        return _strip_diff(self.pixels, self.reference())

    def get_pixel(self, x: int, y: int) -> tuple[int, ...]:
        return tuple(int(v) for v in self.pixels[y, x])

    def add_change_listener(self, fn) -> None:
        """Call ``fn(rect)`` after pixels change; rect is None when the whole image changed."""
        self._listeners.append(fn)

    def remove_change_listener(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _touch(self, rect: Rect | None) -> None:
        self.generation += 1
        if rect is None:
            self._rows_dirty = None
        elif self._rows_dirty is not None:
            self._rows_dirty[rect.y:rect.y1] = True
        for fn in list(self._listeners):
            fn(rect)

    def _finish_stroke(self) -> None:
        if self._stroke is not None:
            self.end_stroke()

    def _check_xy(self, x: int, y: int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"pixel ({x}, {y}) is outside the {self.width}×{self.height} image")

    def _color(self, values) -> np.ndarray:
        values = list(values)
        if len(values) < self.channels:
            raise ValueError(f"{self.mode} needs {self.channels} channel values, got {len(values)}")
        arr = np.array(values[: self.channels], dtype=np.int64)
        if arr.min() < 0 or arr.max() > 255:
            raise ValueError("channel values must be in 0..255")
        return arr.astype(np.uint8)

    def _mask(self, mask) -> np.ndarray:
        if mask is None:
            return np.ones(self.channels, dtype=bool)
        m = np.array(list(mask)[: self.channels], dtype=bool)
        if len(m) != self.channels:
            raise ValueError("channel mask too short")
        return m


    def _record_sparse(self, label: str, ys, xs, before, touched: int) -> OperationSummary:
        after = self.pixels[ys, xs]
        changed = (before != after).any(axis=1)
        ys, xs, before, after = ys[changed], xs[changed], before[changed], after[changed]
        summary = OperationSummary(
            label, int(changed.sum()), int((before != after).sum()), _bbox_of(ys, xs), touched_pixels=touched)
        self.last_operation = summary
        if len(ys) == 0:
            return summary
        ref = self.reference()[ys, xs]
        self._adjust_content_diff(before, after, ref)
        self.history.push(SparsePatch(label, ys=ys, xs=xs, before=before, after=after))
        self._touch(summary.bbox)
        return summary

    def _record_rect(self, label: str, rect: Rect, before: np.ndarray) -> OperationSummary:
        region = self.pixels[rect.y:rect.y1, rect.x:rect.x1]
        neq = region != before
        changed = neq.any(axis=2)
        ys, xs = np.nonzero(changed)
        summary = OperationSummary(
            label, int(changed.sum()), int(neq.sum()), _bbox_of(ys + rect.y, xs + rect.x),
            touched_pixels=rect.w * rect.h)
        self.last_operation = summary
        if summary.changed_pixels == 0:
            return summary
        ref = self.reference()[rect.y:rect.y1, rect.x:rect.x1]
        self._adjust_content_diff(before, region, ref)
        self.history.push(RectPatch(label, x0=rect.x, y0=rect.y, before=before, after=region.copy()))
        self._touch(rect)
        return summary

    def _adjust_content_diff(self, before: np.ndarray, after: np.ndarray, ref: np.ndarray, sign: int = 1) -> None:
        bp, bb = diff_counts(before, ref)
        ap, ab = diff_counts(after, ref)
        self.content_diff_pixels += sign * (ap - bp)
        self.content_diff_bytes += sign * (ab - bb)


    def set_pixel(self, x: int, y: int, values, label: str | None = None) -> OperationSummary:
        """Set every channel of one pixel to exact values. Nothing else is touched."""
        self._finish_stroke()
        self._check_xy(x, y)
        color = self._color(values)
        ys, xs = np.array([y]), np.array([x])
        before = self.pixels[ys, xs]
        self.pixels[y, x] = color
        return self._record_sparse(label or f"Set pixel ({x}, {y})", ys, xs, before, touched=1)

    def begin_stroke(self, color, size: int = 1, mask=None, label: str = "Pencil stroke") -> None:
        if self._stroke is not None:
            self.end_stroke()
        self._stroke = _Stroke(self._color(color), max(1, int(size)), self._mask(mask), label)

    def stroke_to(self, x: int, y: int) -> Rect | None:
        """Extend the current stroke to (x, y). Returns the image rect that was written."""
        s = self._stroke
        if s is None:
            raise RuntimeError("no stroke in progress")
        points = [(x, y)] if s.last is None else bresenham(*s.last, x, y)
        s.last = (x, y)
        lo = -((s.size - 1) // 2)
        hi = lo + s.size
        channels = np.nonzero(s.mask)[0]
        width, height = self.width, self.height
        dirty: Rect | None = None
        for px, py in points:
            dab = Rect(px + lo, py + lo, hi - lo, hi - lo).clamp(width, height)
            if dab.empty:
                continue
            for yy in range(dab.y, dab.y1):
                row = self.pixels[yy]
                base = yy * width
                for xx in range(dab.x, dab.x1):
                    key = base + xx
                    if key not in s.before:
                        s.before[key] = row[xx].copy()
            self.pixels[dab.y:dab.y1, dab.x:dab.x1, channels] = s.color[channels]
            dirty = dab if dirty is None else Rect.from_points(
                min(dirty.x, dab.x), min(dirty.y, dab.y), max(dirty.x1, dab.x1) - 1, max(dirty.y1, dab.y1) - 1)
        if dirty is not None:
            self._touch(dirty)
        return dirty

    def end_stroke(self) -> OperationSummary:
        s = self._stroke
        if s is None:
            raise RuntimeError("no stroke in progress")
        self._stroke = None
        if not s.before:
            summary = OperationSummary(s.label, 0, 0, None)
            self.last_operation = summary
            return summary
        keys = np.fromiter(s.before.keys(), dtype=np.int64, count=len(s.before))
        order = np.argsort(keys)
        keys = keys[order]
        before = np.stack(list(s.before.values()))[order]
        return self._record_sparse(s.label, keys // self.width, keys % self.width, before, touched=len(keys))

    @property
    def stroke_active(self) -> bool:
        return self._stroke is not None

    def fill_rect(self, rect: Rect, color, mask=None, label: str = "Fill selection") -> OperationSummary:
        self._finish_stroke()
        rect = rect.clamp(self.width, self.height)
        if rect.empty:
            raise ValueError("fill rectangle is outside the image")
        color, mask = self._color(color), self._mask(mask)
        channels = np.nonzero(mask)[0]
        before = self.pixels[rect.y:rect.y1, rect.x:rect.x1].copy()
        self.pixels[rect.y:rect.y1, rect.x:rect.x1, channels] = color[channels]
        return self._record_rect(label, rect, before)

    def copy_rect(self, rect: Rect) -> Clip:
        rect = rect.clamp(self.width, self.height)
        if rect.empty:
            raise ValueError("copy rectangle is outside the image")
        return Clip(self.pixels[rect.y:rect.y1, rect.x:rect.x1].copy(), self.mode)

    def paste_channel_note(self, clip: Clip) -> str | None:
        """Explain how a mode mismatch would be handled, or None if modes match."""
        if clip.mode == self.mode:
            return None
        if clip.mode == "RGBA" and self.mode == "RGB":
            return "The clipboard is RGBA but this image is RGB: the clipboard's alpha channel will be discarded."
        return ("The clipboard is RGB but this image is RGBA: only R, G and B are written; "
                "the destination alpha values are left exactly as they are.")

    def paste(self, clip: Clip, x: int, y: int, mask=None, *, allow_channel_mismatch: bool = False,
              label: str = "Paste") -> OperationSummary:
        """Write *clip* with its top-left corner at (x, y), clipped to the image bounds."""
        self._finish_stroke()
        note = self.paste_channel_note(clip)
        if note and not allow_channel_mismatch:
            raise ChannelMismatchError(note)
        mask = self._mask(mask)
        src = clip.pixels
        if clip.mode == "RGBA" and self.mode == "RGB":
            src = src[..., :3]
        elif clip.mode == "RGB" and self.mode == "RGBA":
            mask = mask & np.array([True, True, True, False])
            src = np.concatenate([src, np.zeros(src.shape[:2] + (1,), np.uint8)], axis=2)
        target = Rect(x, y, clip.width, clip.height).clamp(self.width, self.height)
        if target.empty:
            raise ValueError("paste position is entirely outside the image")
        sx, sy = target.x - x, target.y - y
        channels = np.nonzero(mask)[0]
        before = self.pixels[target.y:target.y1, target.x:target.x1].copy()
        region = self.pixels[target.y:target.y1, target.x:target.x1]
        region[..., channels] = src[sy:sy + target.h, sx:sx + target.w][..., channels]
        summary = self._record_rect(label, target, before)
        if (target.w, target.h) != (clip.width, clip.height):
            summary = OperationSummary(
                summary.label, summary.changed_pixels, summary.changed_bytes, summary.bbox,
                summary.touched_pixels,
                note=f"clipped to {target.w}×{target.h} of {clip.width}×{clip.height}; "
                     f"{summary.changed_pixels} pixel(s), {summary.changed_bytes} byte(s) changed"
                     + (f"; bounding box {summary.bbox}" if summary.bbox else ""))
            self.last_operation = summary
        return summary

    def transform(self, op: str) -> OperationSummary:
        """Lossless rotate/flip of the whole image."""
        self._finish_stroke()
        before = (self.width, self.height)
        self.pixels = transforms.apply_transform(self.pixels, op)
        self.orientation = self.orientation.then(op)
        self.history.push(TransformCommand(transforms.LABELS[op], op=op))
        self._touch(None)
        return self._transform_summary(transforms.LABELS[op], before)

    def _transform_summary(self, label: str, before: tuple[int, int]) -> OperationSummary:
        n = self.width * self.height
        summary = OperationSummary(
            label, 0, 0, None,
            note=(f"{n} pixels repositioned, 0 channel values altered; "
                  f"{before[0]}×{before[1]} → {self.width}×{self.height}. "
                  "Pixel byte order changed: payloads decode only after the inverse transform."))
        self.last_operation = summary
        return summary


    def _apply(self, command: Command, forward: bool) -> OperationSummary:
        verb = "Redo" if forward else "Undo"
        if isinstance(command, TransformCommand):
            before = (self.width, self.height)
            op = command.op if forward else transforms.INVERSE[command.op]
            self.pixels = transforms.apply_transform(self.pixels, op)
            self.orientation = self.orientation.then(op)
            self._touch(None)
            return self._transform_summary(f"{verb} {command.label}", before)
        old, new = (command.before, command.after) if forward else (command.after, command.before)
        if isinstance(command, SparsePatch):
            ref = self.reference()[command.ys, command.xs]
            self.pixels[command.ys, command.xs] = new
            bbox = _bbox_of(command.ys, command.xs)
        else:
            h, w = command.before.shape[:2]
            sl = (slice(command.y0, command.y0 + h), slice(command.x0, command.x0 + w))
            ref = self.reference()[sl]
            self.pixels[sl] = new
            changed = (old != new).any(axis=2)
            ys, xs = np.nonzero(changed)
            bbox = _bbox_of(ys + command.y0, xs + command.x0)
        self._adjust_content_diff(old, new, ref)
        if bbox is not None:
            self._touch(bbox)
        neq = old != new
        summary = OperationSummary(f"{verb} {command.label}", int(neq.any(axis=-1).sum()), int(neq.sum()), bbox)
        self.last_operation = summary
        return summary

    def undo(self) -> OperationSummary | None:
        self._finish_stroke()
        command = self.history.pop_undo()
        return None if command is None else self._apply(command, forward=False)

    def redo(self) -> OperationSummary | None:
        self._finish_stroke()
        command = self.history.pop_redo()
        return None if command is None else self._apply(command, forward=True)


    def plan_save(self, dest: str | None = None) -> SavePlan:
        dest = dest or self.path
        if not dest:
            raise ValueError("no destination path")
        return plan_save(
            dest, self.pixels, self.layout,
            pixels_changed=not self.matches_original,
            transposed=self.orientation.transposes,
        )

    def save(self, plan: SavePlan | None = None, *, dest: str | None = None,
             compress_level: int = DEFAULT_COMPRESS_LEVEL) -> SaveReport:
        self._finish_stroke()
        plan = plan or self.plan_save(dest)
        report = save_png_verified(self.pixels, plan, compress_level=compress_level,
                                   reuse=self._encoded, dirty_rows=self._rows_dirty)
        self._encoded = report.encoded
        self._rows_dirty = np.zeros(self.height, dtype=bool)
        self.path = report.dest
        self.saved_state = self.history.state_id
        self.saved_pixel_sha256 = report.pixel_sha256
        self.disk_file_sha256 = report.file_sha256
        return report

    def disk_changed_since_load_or_save(self) -> bool:
        """True if the file at ``path`` is no longer the bytes eXact loaded or wrote."""
        if not self.path or not self.disk_file_sha256:
            return False
        try:
            return file_sha256(self.path) != self.disk_file_sha256
        except FileNotFoundError:
            return True


def _strip_diff(a: np.ndarray, b: np.ndarray, rows: int = 256) -> tuple[int, int]:
    pixels = bytes_ = 0
    for y0 in range(0, a.shape[0], rows):
        p, c = diff_counts(a[y0:y0 + rows], b[y0:y0 + rows])
        pixels += p
        bytes_ += c
    return pixels, bytes_


__all__ = [
    "ChannelMismatchError", "Clip", "Document", "OperationSummary", "Rect", "bresenham", "channels_for_mode",
]
