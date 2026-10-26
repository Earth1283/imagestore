"""Power tools: operations that compute channel values instead of copying them.

Everything else in eXact writes values the user typed or copied. These tools choose the
pixels for you (paint bucket, shapes) or invent new values outright (blur, inversion,
deep-frying, bit flips). They still follow the core contract:

* Qt-free and side-effect free. Every function here only *reads* the buffer and returns a
  result; ``Document`` stays the single writer, so each application is one undoable
  step with exact accounting, and a preview costs nothing to throw away.
* Deterministic. Same buffer and settings, same bytes, on every machine: the blur is
  integer arithmetic throughout, and random tools take an explicit seed.
* Bounded memory. Work is done in row bands or column strips of a few megabytes; no
  float copy of the whole image is ever made. The only full-size allocation is the
  result itself, which becomes the redo half of the history step.

Region results are ``(h, w, C)`` uint8 arrays for a ``Rect``. Painted regions are a bool
mask plus its top-left corner. Sparse results are absolute coordinates plus new values.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

BAND_ELEMENTS = 1 << 21  # working elements per band (8 MB of uint32)
WORKERS = max(1, min(4, os.cpu_count() or 1))  # NumPy releases the GIL in the band loops
MAX_BLUR_RADIUS = 100.0  # keeps every integer window sum below 2**32 (see blur_plan)
SHAPES = ("line", "rect", "ellipse")
_EMPTY = 1 << 40  # span bound marking an empty row


def _rows_per_band(row_elements: int, align: int = 1) -> int:
    rows = max(1, BAND_ELEMENTS // max(1, row_elements))
    return max(align, rows - rows % align)


def _parallel(fn, starts) -> None:
    """Run *fn* over independent bands on a few threads. Every band writes a disjoint
    part of the output, so the result does not depend on scheduling."""
    starts = list(starts)
    if WORKERS == 1 or len(starts) < 2:
        for start in starts:
            fn(start)
        return
    with ThreadPoolExecutor(WORKERS) as pool:
        for _ in pool.map(fn, starts):
            pass


def _channel_index(chs: list[int]):
    """A basic slice for contiguous channels (fast, no copies), else the list."""
    if chs == list(range(chs[0], chs[-1] + 1)):
        return slice(chs[0], chs[-1] + 1)
    return chs


# ------------------------------------------------------------------------------ spans


def spans_to_mask(ys, x0s, x1s, width: int, height: int):
    """Clip half-open row spans ``[x0, x1)`` on rows ``ys`` to the image and rasterize
    them. Returns ``(left, top, mask)`` for the bounding box, or None if nothing is left."""
    ys, x0s, x1s = (np.asarray(a, dtype=np.int64) for a in (ys, x0s, x1s))
    keep = (ys >= 0) & (ys < height)
    ys, x0s, x1s = ys[keep], np.clip(x0s[keep], 0, width), np.clip(x1s[keep], 0, width)
    keep = x1s > x0s
    ys, x0s, x1s = ys[keep], x0s[keep], x1s[keep]
    if len(ys) == 0:
        return None
    top, left = int(ys.min()), int(x0s.min())
    h, w = int(ys.max()) + 1 - top, int(x1s.max()) - left
    lengths = x1s - x0s
    count = int(lengths.sum())
    if count * 8 < h * w:
        # thin shapes (outlines, lines): set the pixels by flat index, O(pixels) memory
        mask = np.zeros(h * w, dtype=bool)
        starts = (ys - top) * w + (x0s - left)
        mask[np.repeat(starts - np.cumsum(lengths) + lengths, lengths) + np.arange(count)] = True
        return left, top, mask.reshape(h, w)
    # dense shapes: +1/-1 at span edges, running sum along each row (spans may overlap)
    edges = np.zeros((h, w + 1), np.int8)
    np.add.at(edges, (ys - top, x0s - left), 1)
    np.add.at(edges, (ys - top, x1s - left), -1)
    return left, top, np.cumsum(edges, axis=1, dtype=np.int8)[:, :w] > 0


def _row_spans(lo: np.ndarray, hi: np.ndarray, top: int):
    rows = np.flatnonzero(hi > lo)
    return rows + top, lo[rows], hi[rows]


def _erode_rows(lo: np.ndarray, hi: np.ndarray, radius: int, cross: bool = False):
    """Erode a shape given as one span per row (empty rows have lo >= hi).

    Square erosion: x survives iff [x-r, x+r] fits in every row y-r..y+r, so the new span
    is [max(lo) + r, min(hi) - r) over that window. Cross erosion (radius 1) keeps x iff
    its four neighbours are inside, which leaves a thin, 8-connected outline.
    """
    n = len(lo)
    pad_lo = np.concatenate([np.full(radius, _EMPTY), lo, np.full(radius, _EMPTY)])
    pad_hi = np.concatenate([np.full(radius, -_EMPTY), hi, np.full(radius, -_EMPTY)])
    if cross:
        new_lo = np.maximum.reduce([lo + 1, pad_lo[:n], pad_lo[2:n + 2]])
        new_hi = np.minimum.reduce([hi - 1, pad_hi[:n], pad_hi[2:n + 2]])
        return new_lo, new_hi
    new_lo, new_hi = lo.copy(), hi.copy()
    for k in range(2 * radius + 1):
        np.maximum(new_lo, pad_lo[k:k + n], out=new_lo)
        np.minimum(new_hi, pad_hi[k:k + n], out=new_hi)
    return new_lo + radius, new_hi - radius


def shape_spans(kind: str, ax: int, ay: int, bx: int, by: int, stroke: int = 1, filled: bool = False):
    """Aliased row spans of a line, rectangle or ellipse between two pixels (inclusive).

    Every pixel is either fully written or untouched. A line is the pencil's own stroke
    (Bresenham with a square dab of side *stroke*); outlines grow inward from the edge.
    """
    stroke = max(1, int(stroke))
    if kind == "line":
        from .document import bresenham

        pts = np.array(bresenham(ax, ay, bx, by), dtype=np.int64)
        lo_off = -((stroke - 1) // 2)
        top = int(pts[:, 1].min()) + lo_off
        rows = int(pts[:, 1].max()) - int(pts[:, 1].min()) + stroke
        lo, hi = np.full(rows, _EMPTY), np.full(rows, -_EMPTY)
        for d in range(stroke):
            r = pts[:, 1] + lo_off + d - top
            np.minimum.at(lo, r, pts[:, 0] + lo_off)
            np.maximum.at(hi, r, pts[:, 0] + lo_off + stroke)
        return _row_spans(lo, hi, top)
    x0, x1 = min(ax, bx), max(ax, bx) + 1
    y0, y1 = min(ay, by), max(ay, by) + 1
    rows = y1 - y0
    if kind == "rect":
        lo, hi = np.full(rows, x0, np.int64), np.full(rows, x1, np.int64)
    elif kind == "ellipse":
        a, b = (x1 - x0) / 2, rows / 2
        t = (np.arange(rows) + 0.5 - b) / b
        half = a * np.sqrt(np.clip(1 - t * t, 0, None))
        cx = x0 + a
        lo = np.ceil(cx - half - 0.5).astype(np.int64)
        hi = np.floor(cx + half - 0.5).astype(np.int64) + 1
    else:
        raise ValueError(f"unknown shape {kind!r}")
    if filled:
        return _row_spans(lo, hi, y0)
    valid = hi > lo
    lo, hi = np.where(valid, lo, _EMPTY), np.where(valid, hi, -_EMPTY)
    inner_lo, inner_hi = _erode_rows(lo, hi, stroke - 1) if stroke > 1 else (lo, hi)
    inner_lo, inner_hi = _erode_rows(inner_lo, inner_hi, 1, cross=True)
    hollow = inner_hi > inner_lo
    # outline = filled span minus the inner span: one or two pieces per row
    ys = np.concatenate([np.arange(rows), np.flatnonzero(hollow)]) + y0
    x0s = np.concatenate([lo, inner_hi[hollow]])
    x1s = np.concatenate([np.where(hollow, inner_lo, hi), hi[hollow]])
    keep = x1s > x0s
    return ys[keep], x0s[keep], x1s[keep]


def flood_fill(pixels: np.ndarray, x: int, y: int, tolerance: int = 0, contiguous: bool = True):
    """Pixels the paint bucket would fill from (x, y), as ``(left, top, mask)``.

    A pixel matches when *every* channel is within ±tolerance of the clicked pixel (alpha
    included: transparent is a value, not an absence). Contiguous fills grow 4-connected
    through whole runs of matching pixels, so the cost follows the number of runs, not
    pixels, and rows are only compared when the fill reaches them.
    """
    height, width = pixels.shape[:2]
    seed = pixels[y, x].astype(np.int16)

    def match(rows: np.ndarray) -> np.ndarray:
        if tolerance <= 0:
            return (rows == pixels[y, x]).all(axis=-1)
        return (np.abs(rows.astype(np.int16) - seed) <= tolerance).all(axis=-1)

    if not contiguous:
        mask = np.empty((height, width), dtype=bool)
        band = _rows_per_band(width * pixels.shape[2])
        for r0 in range(0, height, band):
            mask[r0:r0 + band] = match(pixels[r0:r0 + band])
        rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
        top, left = int(rows[0]), int(cols[0])
        return left, top, mask[top:rows[-1] + 1, left:cols[-1] + 1]

    runs: dict[int, tuple] = {}

    def row_runs(yy: int):
        r = runs.get(yy)
        if r is None:
            edges = np.flatnonzero(np.diff(np.concatenate(([0], match(pixels[yy]).view(np.int8), [0]))))
            r = runs[yy] = (edges[0::2], edges[1::2], np.zeros(len(edges) // 2, dtype=bool))
        return r

    starts, ends, seen = row_runs(y)
    i = int(np.searchsorted(ends, x, side="right"))
    seen[i] = True
    stack = [(y, int(starts[i]), int(ends[i]))]
    out_y, out_s, out_e = [], [], []
    while stack:
        yy, s, e = stack.pop()
        out_y.append(yy)
        out_s.append(s)
        out_e.append(e)
        for ny in (yy - 1, yy + 1):
            if 0 <= ny < height:
                ns, ne, nseen = row_runs(ny)
                # runs on the next row that overlap [s, e)
                for j in range(int(np.searchsorted(ne, s, side="right")), int(np.searchsorted(ns, e, side="left"))):
                    if not nseen[j]:
                        nseen[j] = True
                        stack.append((ny, int(ns[j]), int(ne[j])))
    return spans_to_mask(out_y, out_s, out_e, width, height)


# ------------------------------------------------------------------------------ blur


def blur_plan(radius: float):
    """Integer separable passes approximating a Gaussian of standard deviation *radius*.

    Below 2 px: one exact sampled kernel (weights scaled to integers), so small radii can
    be tuned finely. From 2 px: three box blurs, the approximation the SVG and CSS filter
    specifications use. Returns ``(passes, divisor, reach)``: a pass is ``("box", size)``
    or ``("kernel", weights)``, the summed result is divided by *divisor* once per axis,
    and *reach* is how far (in pixels) the result depends on its neighbours.
    """
    sigma = min(max(float(radius), 0.0), MAX_BLUR_RADIUS)
    if sigma < 2:
        reach = max(1, math.ceil(3 * sigma))
        w = np.round(4096 * np.exp(-np.arange(-reach, reach + 1) ** 2 / (2 * max(sigma, 1e-6) ** 2)))
        w = w.astype(np.uint32)
        return [("kernel", w)], int(w.sum()), reach
    ideal = math.sqrt(12 * sigma * sigma / 3 + 1)
    lower = int(ideal) - (1 - int(ideal) % 2)
    m = min(3, max(0, round((12 * sigma * sigma - 3 * lower * lower - 12 * lower - 9) / (-4 * lower - 4))))
    sizes = [lower if i < m else lower + 2 for i in range(3)]
    # Worst-case window sum: 255 * 201**3 (radius 100) < 2**32, so uint32 sums are exact.
    return [("box", s) for s in sizes], int(np.prod(sizes)), sum((s - 1) // 2 for s in sizes)


def _window(a: np.ndarray, axis: int, start: int, stop: int) -> np.ndarray:
    index = [slice(None)] * a.ndim
    index[axis] = slice(start, stop)
    return a[tuple(index)]


def _run_passes(a: np.ndarray, passes, axis: int) -> np.ndarray:
    """Valid (shrinking) convolution of uint32 *a* along *axis* by every pass."""
    for kind, arg in passes:
        n = a.shape[axis]
        if kind == "box":
            # Running sums wrap modulo 2**32; window differences of them are still exact.
            c = np.cumsum(a, axis=axis, dtype=np.uint32)
            out = _window(c, axis, arg - 1, n).copy()
            tail = _window(out, axis, 1, None)
            tail -= _window(c, axis, 0, n - arg)
        else:
            k = len(arg)
            out = _window(a, axis, 0, n - k + 1) * arg[0]
            for i in range(1, k):
                out += _window(a, axis, i, n - k + 1 + i) * arg[i]
        a = out
    return a


def _divide(a: np.ndarray, divisor: int) -> np.ndarray:
    return ((a + np.uint32(divisor // 2)) // np.uint32(divisor)).astype(np.uint8)


def _padded(a: np.ndarray, axis: int, before: int, after: int) -> np.ndarray:
    width = [(0, 0)] * a.ndim
    width[axis] = (before, after)
    return np.pad(a, width, mode="edge") if before or after else a


def gaussian_blur(src: np.ndarray, rect, radius: float, strength: float = 100.0, channels=None) -> np.ndarray:
    """Blur *rect* of *src*; returns the new ``(h, w, C)`` region and leaves *src* alone.

    Neighbours outside the rectangle are read (a selection blurs into its surroundings
    the way you would expect) and the image edge is extended by repetition. *strength*
    (0–100 %, in steps of 0.1) mixes the blur with the original values. Only *channels*
    are computed; every other channel is copied through unchanged.

    Horizontal passes run in row bands, vertical passes in column strips, each rounding
    once, so working memory is a few bands regardless of image size or radius.
    """
    h_img, w_img, nch = src.shape
    chs = list(range(nch)) if channels is None else [c for c in channels if c < nch]
    out = src[rect.y:rect.y1, rect.x:rect.x1].copy()
    passes, divisor, reach = blur_plan(radius)
    mix = int(round(min(max(strength, 0.0), 100.0) * 10))
    if not chs or mix == 0:
        return out
    k, ci = len(chs), _channel_index(chs)
    # rows whose horizontal blur the vertical pass needs: the rect plus reach above and below
    y_lo, y_hi = max(0, rect.y - reach), min(h_img, rect.y1 + reach)
    x_lo, x_hi = max(0, rect.x - reach), min(w_img, rect.x1 + reach)
    pad_l, pad_r = reach - (rect.x - x_lo), reach - (x_hi - rect.x1)
    above = np.empty((rect.y - y_lo, rect.w, k), np.uint8)
    below = np.empty((y_hi - rect.y1, rect.w, k), np.uint8)
    band = _rows_per_band((x_hi - x_lo + 2 * reach) * k * 2)

    def horizontal(r0: int) -> None:
        r1 = min(y_hi, r0 + band)
        seg = _padded(src[r0:r1, x_lo:x_hi, ci].astype(np.uint32), 1, pad_l, pad_r)
        res = _divide(_run_passes(seg, passes, 1), divisor)
        for dest, d0, d1 in ((above, y_lo, rect.y), (None, rect.y, rect.y1), (below, rect.y1, y_hi)):
            a, b = max(r0, d0), min(r1, d1)
            if a < b:
                if dest is None:
                    out[a - rect.y:b - rect.y, :, ci] = res[a - r0:b - r0]
                else:
                    dest[a - d0:b - d0] = res[a - r0:b - r0]

    _parallel(horizontal, range(y_lo, y_hi, band))
    pad_t, pad_b = reach - len(above), reach - len(below)
    strip = _rows_per_band((rect.h + 2 * reach) * k * 2)

    def vertical(c0: int) -> None:
        c1 = min(rect.w, c0 + strip)
        col = np.concatenate([above[:, c0:c1], out[:, c0:c1, ci], below[:, c0:c1]]).astype(np.uint32)
        blurred = _divide(_run_passes(_padded(col, 0, pad_t, pad_b), passes, 0), divisor)
        if mix < 1000:
            orig = src[rect.y:rect.y1, rect.x + c0:rect.x + c1, ci].astype(np.uint32)
            blurred = ((orig * (1000 - mix) + blurred.astype(np.uint32) * mix + 500) // 1000).astype(np.uint8)
        out[:, c0:c1, ci] = blurred

    _parallel(vertical, range(0, rect.w, strip))
    return out


# --------------------------------------------------------------------- value filters


def invert(src: np.ndarray, rect, channels=None) -> np.ndarray:
    """255 − v in the chosen channels. Its own inverse: apply twice, get every byte back."""
    out = src[rect.y:rect.y1, rect.x:rect.x1].copy()
    for c in range(out.shape[2]) if channels is None else channels:
        if c < out.shape[2]:
            np.subtract(255, out[..., c], out=out[..., c])
    return out


NUKE_LEVELS = ((20, "Lightly toasted"), (40, "Deep fried"), (60, "Extra crispy"), (85, "Nuked"),
               (101, "Thermonuclear"))


def nuke_level(heat: float) -> str:
    return next(name for limit, name in NUKE_LEVELS if heat < limit)


def _hash_noise(xs: np.ndarray, ys: np.ndarray, seed: int) -> np.ndarray:
    """Per-pixel noise in [-1, 1] from absolute coordinates, so a preview of any crop
    shows exactly the grain the full image will get."""
    with np.errstate(over="ignore"):
        h = (xs[None, :].astype(np.uint32) * np.uint32(0x27D4EB2D)) ^ (ys[:, None].astype(np.uint32)
                                                                     * np.uint32(0x165667B1))
        h ^= np.uint32(seed & 0xFFFFFFFF)
        h ^= h >> np.uint32(15)
        h *= np.uint32(0x2C1B3C6D)
        h ^= h >> np.uint32(12)
        h *= np.uint32(0x297A2D39)
        h ^= h >> np.uint32(15)
    return (h & np.uint32(0xFFFF)).astype(np.float32) / 32767.5 - 1


def _block_means(a: np.ndarray, x0: int, y0: int, block: int) -> np.ndarray:
    """Replace each block×block cell (aligned to absolute coordinates) by its mean."""
    def starts(origin, n):
        first = (origin // block + 1) * block
        return np.concatenate(([0], np.arange(first, origin + n, block) - origin))

    rs, cs = starts(y0, a.shape[0]), starts(x0, a.shape[1])
    rn, cn = np.diff(np.append(rs, a.shape[0])), np.diff(np.append(cs, a.shape[1]))
    sums = np.add.reduceat(np.add.reduceat(a, rs, axis=0), cs, axis=1)
    means = sums / (rn[:, None] * cn[None, :])
    return np.repeat(np.repeat(means, rn, axis=0), cn, axis=1)


def nuke(src: np.ndarray, rect, heat: float = 60.0, channels=None, seed: int = 921) -> np.ndarray:
    """The photo nuker: saturation, contrast, posterisation, blocky chroma, grain and a
    radioactive tint, all scaled by *heat* (0–100 %). Alpha is never touched; even we
    have limits."""
    h = min(max(heat, 0.0), 100.0) / 100
    chs = [c for c in (range(3) if channels is None else channels) if c < 3]
    out = src[rect.y:rect.y1, rect.x:rect.x1].copy()
    if not chs or h == 0:
        return out
    sat, contrast, grain, tint = 1 + 4 * h, 1 + 1.8 * h, 64 * h, 56 * h
    levels = 2 ** round(8 - 6 * h) - 1
    block = 0 if h < .25 else 4 if h < .5 else 8 if h < .8 else 16
    # Work on whole 16-pixel cells of the absolute grid, so every chroma block is averaged
    # over its full extent whatever the target: a preview crop is exactly the final result.
    ex0, ex1 = rect.x - rect.x % 16, min(src.shape[1], -(-rect.x1 // 16) * 16)
    band = _rows_per_band((ex1 - ex0) * 12, align=16)
    xs = np.arange(ex0, ex1)

    def fry(r0: int) -> None:
        a, b = max(r0, rect.y), min(r0 + band, rect.y1)
        ea, eb = r0, min(r0 + band, src.shape[0])
        rgb = src[ea:eb, ex0:ex1, :3].astype(np.float32)
        luma = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]  # elementwise: no BLAS ordering
        cb, cr = rgb[..., 2] - luma, rgb[..., 0] - luma
        if block:
            cb, cr = _block_means(cb, ex0, ea, block), _block_means(cr, ex0, ea, block)
        luma = (luma - 128) * contrast + 128 + grain * _hash_noise(xs, np.arange(ea, eb), seed)
        r, bl = luma + cr * sat + tint, luma + cb * sat - tint
        g = (luma - 0.299 * r - 0.114 * bl) / 0.587 + 0.35 * tint
        fried = np.stack([r, g, bl], axis=-1)
        fried = np.round(np.round(np.clip(fried, 0, 255) * (levels / 255)) * (255 / levels))
        fried = fried[a - ea:b - ea, rect.x - ex0:rect.x1 - ex0]
        out[a - rect.y:b - rect.y, :, ci] = fried[..., ci].astype(np.uint8)

    ci = _channel_index(chs)
    _parallel(fry, range(rect.y - rect.y % 16, rect.y1, band))
    return out


def cosmic_ray(src: np.ndarray, rect, count: int, seed: int = 921, channels=None):
    """Flip *count* distinct random bits inside *rect* (only in *channels*).

    Returns ``(ys, xs, values)`` for the pixels that change, in absolute coordinates.
    Useful for proving that an ImageStore payload's SHA-256 notices.
    """
    nch = src.shape[2]
    chs = np.array([c for c in (range(nch) if channels is None else channels) if c < nch], dtype=np.int64)
    total = rect.w * rect.h * len(chs) * 8
    count = int(min(max(count, 0), total))
    if count == 0:
        empty = np.zeros(0, np.int64)
        return empty, empty, np.zeros((0, nch), np.uint8)
    bits = np.random.default_rng(seed).choice(total, size=count, replace=False, shuffle=False)
    pixel, rest = np.divmod(bits, len(chs) * 8)
    channel, bit = chs[rest // 8], rest % 8
    order = np.argsort(pixel, kind="stable")
    pixel, channel, bit = pixel[order], channel[order], bit[order]
    unique, inverse = np.unique(pixel, return_inverse=True)
    ys, xs = unique // rect.w + rect.y, unique % rect.w + rect.x
    flips = np.zeros((len(unique), nch), np.uint8)
    np.bitwise_or.at(flips, (inverse, channel), (1 << bit).astype(np.uint8))
    return ys, xs, src[ys, xs] ^ flips
