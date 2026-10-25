"""Strict PNG loading and verified, atomic PNG saving.

Loading
    The chunk structure is parsed first (png_chunks) and only 8-bit truecolour (RGB,
    colour type 2) and 8-bit truecolour+alpha (RGBA, colour type 6) are accepted. The
    image data is then decoded by Pillow, in horizontal strips, directly into a canonical
    buffer. Nothing is converted: no profile, gamma, EXIF orientation or alpha handling.

Saving
    eXact encodes IDAT itself (vectorised PNG filtering + zlib) so it fully controls
    what goes into the file and can carry ancillary chunks through verbatim. The file is
    written to a temporary file next to the destination, re-parsed, decoded with Pillow
    (an independent decoder) and compared byte-for-byte against the intended buffer.
    Only then is the destination atomically replaced.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import tempfile
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import BinaryIO

import numpy as np
from PIL import Image

from .png_chunks import (
    APNG_CHUNKS,
    IHDR,
    KNOWN_ANCILLARY,
    PNG_SIGNATURE,
    Chunk,
    ChunkLayout,
    PngFormatError,
    read_chunk_layout,
    write_chunk,
)
from .pixels import buffer_sha256, channels_for_mode, validate_buffer

# Pillow's decompression-bomb guard triggers around 89 MP; eXact is meant for large
# payload images, so raise the ceiling (still finite) for files the user opens on purpose.
Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, 1 << 30)
# Pillow allocates decoded images in 16 MiB blocks by default, which macOS malloc keeps
# resident after they are freed (~+225 MB RSS after opening a 7488×7487 RGBA image).
# One contiguous allocation is returned to the OS as soon as the decoder image is freed.
Image.core.set_block_size(max(Image.core.get_block_size(), 1 << 30))

COLOR_TYPE_FOR_MODE = {"RGB": 2, "RGBA": 6}
MODE_FOR_COLOR_TYPE = {2: "RGB", 6: "RGBA"}
STRIP_ROWS = 256
IDAT_CHUNK_SIZE = 1 << 18
DEFAULT_COMPRESS_LEVEL = 6

# Chunks whose meaning is purely about colour interpretation. eXact never applies them
# to the pixel data; they are preserved and shown as information only.
COLOR_INTERPRETATION_CHUNKS = {b"iCCP", b"sRGB", b"gAMA", b"cHRM", b"cICP", b"mDCV", b"cLLI"}


class UnsupportedPngError(ValueError):
    """The PNG is valid but its pixel format is outside what eXact can edit losslessly."""


class SaveVerificationError(RuntimeError):
    """The written file did not decode to exactly the intended pixel bytes."""


@dataclass
class LoadedPng:
    path: str
    pixels: np.ndarray
    mode: str
    layout: ChunkLayout
    warnings: list[str] = field(default_factory=list)


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


_UMASK = _umask()


def describe_unsupported(ihdr: IHDR) -> str | None:
    """Explain why *ihdr* is unsupported, or return None if it is supported."""
    if ihdr.color_type not in MODE_FOR_COLOR_TYPE:
        return (
            f"Colour type {ihdr.color_type} ({ihdr.color_type_name}) with bit depth "
            f"{ihdr.bit_depth} is not supported. eXact v0.1.0 edits only 8-bit RGB and "
            "8-bit RGBA PNGs and will not convert the image, because conversion would "
            "change channel values or layout."
        )
    if ihdr.bit_depth != 8:
        return (
            f"{ihdr.bit_depth}-bit {ihdr.color_type_name} is not supported. eXact v0.1.0 "
            "edits only 8-bit-per-channel images; reducing the bit depth would destroy data."
        )
    if ihdr.compression != 0 or ihdr.filter_method != 0 or ihdr.interlace not in (0, 1):
        return "IHDR declares a non-standard compression, filter or interlace method."
    return None


def load_png(path: str) -> LoadedPng:
    """Load an 8-bit RGB/RGBA PNG into a canonical buffer without any conversion."""
    path = os.path.abspath(path)
    layout = read_chunk_layout(path)
    ihdr = layout.ihdr
    problem = describe_unsupported(ihdr)
    if problem:
        raise UnsupportedPngError(problem)
    mode = MODE_FOR_COLOR_TYPE[ihdr.color_type]
    unknown_critical = [
        c.name for c in layout.before_idat + layout.after_idat
        if c.is_critical and c.type != b"PLTE"
    ]
    if unknown_critical:
        raise UnsupportedPngError(
            f"PNG contains unknown critical chunk(s) {', '.join(unknown_critical)}; "
            "the image cannot be decoded reliably."
        )

    channels = channels_for_mode(mode)
    width, height = ihdr.width, ihdr.height
    with Image.open(path) as im:
        if im.format != "PNG":
            raise UnsupportedPngError(f"Pillow identified the file as {im.format}, not PNG")
        if im.mode != mode or im.size != (width, height):
            raise UnsupportedPngError(
                f"Decoder produced mode {im.mode} {im.size} but IHDR declares "
                f"{mode} {(width, height)}; refusing to guess."
            )
        im.load()
        pixels = np.empty((height, width, channels), dtype=np.uint8)
        for y0 in range(0, height, STRIP_ROWS):
            y1 = min(height, y0 + STRIP_ROWS)
            strip = im.crop((0, y0, width, y1)).tobytes()
            pixels[y0:y1] = np.frombuffer(strip, dtype=np.uint8).reshape(y1 - y0, width, channels)

    return LoadedPng(path=path, pixels=pixels, mode=mode, layout=layout, warnings=load_warnings(layout, mode))


def load_warnings(layout: ChunkLayout, mode: str) -> list[str]:
    warnings: list[str] = []
    present = {c.type for c in layout.before_idat + layout.after_idat}
    color_chunks = sorted(c.decode() for c in present & COLOR_INTERPRETATION_CHUNKS)
    if color_chunks:
        warnings.append(
            f"Colour-interpretation chunk(s) present ({', '.join(color_chunks)}). They are kept "
            "as metadata but never applied: eXact shows and edits raw channel values."
        )
    if layout.ihdr.interlace == 1:
        warnings.append(
            "Image is Adam7-interlaced. Pixel values load exactly, but eXact writes "
            "non-interlaced PNGs, so the encoded file layout will change on save."
        )
    if present & APNG_CHUNKS:
        warnings.append(
            "Animated PNG (APNG) detected. Only the default image is loaded; animation "
            "frames cannot be preserved and will be dropped if you save."
        )
    if b"eXIf" in present:
        warnings.append("EXIF data present. Any EXIF orientation tag is ignored (pixels are never auto-rotated).")
    if b"tRNS" in present and mode == "RGB":
        warnings.append(
            "RGB image has a tRNS (single transparent colour) chunk. It is preserved, but "
            "eXact displays and edits the RGB values without applying it."
        )
    if layout.crc_errors:
        warnings.append(f"CRC mismatch in chunk(s): {', '.join(layout.crc_errors)}. The file may be corrupted.")
    if layout.trailing_bytes:
        warnings.append(f"{layout.trailing_bytes} byte(s) of data after IEND will not be preserved on save.")
    return warnings


@dataclass(frozen=True)
class ChunkDecision:
    name: str
    action: str  # "preserved" | "updated" | "dropped" | "regenerated"
    reason: str = ""


@dataclass
class SavePlan:
    dest: str
    mode: str
    ihdr: IHDR
    before_idat: list[Chunk]
    after_idat: list[Chunk]
    decisions: list[ChunkDecision]

    @property
    def dropped(self) -> list[ChunkDecision]:
        return [d for d in self.decisions if d.action == "dropped"]

    @property
    def lossy_metadata(self) -> bool:
        return bool(self.dropped)


def _png_time_now() -> bytes:
    t = time.gmtime()
    return struct.pack(">HBBBBB", t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def plan_save(
    dest: str,
    pixels: np.ndarray,
    layout: ChunkLayout | None,
    *,
    pixels_changed: bool,
    transposed: bool,
) -> SavePlan:
    """Decide the chunk layout of the output file.

    * IHDR/IDAT/IEND are regenerated from the buffer (8-bit, original colour type,
      non-interlaced).
    * Known ancillary chunks are preserved byte-for-byte; pHYs is swapped when the image
      is transposed and tIME is refreshed when pixels changed.
    * Unknown safe-to-copy chunks are preserved. Unknown unsafe-to-copy chunks are kept
      only when the pixel data is unchanged (PNG specification, section 5.4).
    * APNG animation chunks and digital signatures (dSIG) are dropped.
    """
    mode = validate_buffer(pixels)
    height, width = pixels.shape[:2]
    ihdr = IHDR(width, height, 8, COLOR_TYPE_FOR_MODE[mode], 0, 0, 0)
    decisions = [ChunkDecision("IHDR", "regenerated", f"{width}x{height}, 8-bit {mode}, non-interlaced")]
    if layout is not None and layout.ihdr.interlace:
        decisions.append(ChunkDecision("IHDR", "updated", "Adam7 interlacing removed (pixel values unaffected)"))
    before: list[Chunk] = []
    after: list[Chunk] = []
    if layout is not None:
        for target, chunks in ((before, layout.before_idat), (after, layout.after_idat)):
            for chunk in chunks:
                kept = _decide_chunk(chunk, pixels_changed=pixels_changed, transposed=transposed)
                decisions.append(kept[1])
                if kept[0] is not None:
                    target.append(kept[0])
        if layout.trailing_bytes:
            decisions.append(ChunkDecision("(after IEND)", "dropped", f"{layout.trailing_bytes} trailing bytes"))
    decisions.append(ChunkDecision("IDAT", "regenerated", "re-encoded from the authoritative pixel buffer"))
    decisions.append(ChunkDecision("IEND", "regenerated"))
    return SavePlan(os.path.abspath(dest), mode, ihdr, before, after, decisions)


def _decide_chunk(chunk: Chunk, *, pixels_changed: bool, transposed: bool) -> tuple[Chunk | None, ChunkDecision]:
    name = chunk.name
    if chunk.type in APNG_CHUNKS:
        return None, ChunkDecision(name, "dropped", "APNG animation is not supported")
    if chunk.type == b"dSIG":
        return None, ChunkDecision(name, "dropped", "digital signature is invalid after re-encoding")
    if chunk.type == b"PLTE":
        return chunk, ChunkDecision(name, "preserved", "suggested palette (truecolour image)")
    if chunk.type == b"pHYs" and transposed and len(chunk.data) == 9:
        x, y, unit = struct.unpack(">IIB", chunk.data)
        if x != y:
            return Chunk(b"pHYs", struct.pack(">IIB", y, x, unit)), ChunkDecision(
                name, "updated", "X/Y pixel density swapped to match 90° rotation")
    if chunk.type == b"tIME" and pixels_changed:
        return Chunk(b"tIME", _png_time_now()), ChunkDecision(name, "updated", "modification time set to now (UTC)")
    if chunk.type == b"eXIf" and transposed:
        return chunk, ChunkDecision(name, "preserved", "EXIF dimension/orientation tags were NOT updated")
    if chunk.type in KNOWN_ANCILLARY or chunk.is_safe_to_copy:
        return chunk, ChunkDecision(name, "preserved")
    if not pixels_changed:
        return chunk, ChunkDecision(name, "preserved", "unknown unsafe-to-copy chunk; pixel data unchanged")
    return None, ChunkDecision(
        name, "dropped", "unknown chunk marked unsafe-to-copy; PNG spec forbids copying it after image edits")


SCORE_SPAN = 2048
SCORE_SPANS = 4


def _candidates(x: np.ndarray, up: np.ndarray, bpp: int, lead: int) -> np.ndarray:
    """All five filtered versions (5, rows, cols) of int16 rows *x* given the rows above.

    The first *lead* columns are context only (the bytes to the left of the region) and
    are dropped from the result.
    """
    left = np.zeros_like(x)
    left[:, bpp:] = x[:, :-bpp]
    upleft = np.zeros_like(x)
    upleft[:, bpp:] = up[:, :-bpp]
    p = left + up - upleft
    pa, pb, pc = np.abs(p - left), np.abs(p - up), np.abs(p - upleft)
    paeth = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, upleft))
    out = np.stack([x, x - left, x - up, x - ((left + up) >> 1), x - paeth])
    return out[:, :, lead:].astype(np.uint8)


def _choose_filters(block: np.ndarray, up: np.ndarray, bpp: int) -> np.ndarray:
    """Pick a filter type per row with the minimum-sum-of-absolute-differences heuristic,
    scored on a few evenly spaced column spans rather than the whole row."""
    stride = block.shape[1]
    if stride <= SCORE_SPAN * SCORE_SPANS:
        spans = [(0, stride)]
    else:
        step = stride // SCORE_SPANS
        spans = [(i * step - i * step % bpp, i * step - i * step % bpp + SCORE_SPAN) for i in range(SCORE_SPANS)]
    scores = np.zeros((5, block.shape[0]), dtype=np.int64)
    for a, b in spans:
        lead = bpp if a else 0
        x = block[:, a - lead:b].astype(np.int16)
        u = up[:, a - lead:b].astype(np.int16)
        cand = _candidates(x, u, bpp, lead)
        scores += np.abs(cand.view(np.int8).astype(np.int16)).sum(axis=2)
    return scores.argmin(axis=0)


def _filter_block(block: np.ndarray, prev: np.ndarray, bpp: int) -> np.ndarray:
    """Adaptively filter *block* (rows × stride bytes), *prev* being the row above it.

    Returns (rows, 1 + stride): the filter-type byte followed by the filtered row.
    Encoding filters only read unfiltered input, so rows are independent and filters can
    be computed vectorised; only the chosen filter is computed over the full row.
    """
    rows, stride = block.shape
    up = np.empty_like(block)
    up[0] = prev
    up[1:] = block[:-1]
    choice = _choose_filters(block, up, bpp)
    out = np.empty((rows, stride + 1), dtype=np.uint8)
    out[:, 0] = choice
    body = out[:, 1:]
    for f in np.unique(choice):
        idx = np.nonzero(choice == f)[0]
        x, u = block[idx], up[idx]
        if f == 0:
            body[idx] = x
        elif f == 1:
            body[idx, :bpp] = x[:, :bpp]
            body[idx, bpp:] = x[:, bpp:] - x[:, :-bpp]
        elif f == 2:
            body[idx] = x - u
        elif f == 3:
            left = np.zeros_like(x)
            left[:, bpp:] = x[:, :-bpp]
            body[idx] = x - ((left.astype(np.uint16) + u) >> 1).astype(np.uint8)
        else:
            body[idx] = _candidates(x.astype(np.int16), u.astype(np.int16), bpp, 0)[4]
    return out


ADLER_BASE = 65521
FINAL_EMPTY_DEFLATE_BLOCK = b"\x03\x00"


def adler32_combine(adler1: int, adler2: int, len2: int) -> int:
    """Adler-32 of A+B from adler32(A), adler32(B) and len(B) (port of zlib's adler32_combine)."""
    rem = len2 % ADLER_BASE
    sum1 = adler1 & 0xFFFF
    sum2 = (rem * sum1) % ADLER_BASE
    sum1 += (adler2 & 0xFFFF) + ADLER_BASE - 1
    sum2 += ((adler1 >> 16) & 0xFFFF) + ((adler2 >> 16) & 0xFFFF) + ADLER_BASE - rem
    sum1 %= ADLER_BASE
    sum2 %= ADLER_BASE
    return sum1 | (sum2 << 16)


def _deflate_band(band: np.ndarray, prev: np.ndarray, bpp: int, level: int) -> tuple[bytes, int, int]:
    """Filter and raw-deflate one band of rows → (deflate bytes, adler32 of filtered bytes, length).

    Every band is an independent raw deflate segment ending in a sync flush (byte aligned,
    not final), so bands concatenate into one valid stream, can be compressed in
    parallel, and an unchanged band's bytes can be reused verbatim by a later save.
    """
    raw = _filter_block(band, prev, bpp)
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
    data = compressor.compress(raw) + compressor.flush(zlib.Z_SYNC_FLUSH)
    return data, zlib.adler32(raw), raw.size


def _zlib_header(level: int) -> bytes:
    flevel = 0 if level < 2 else 1 if level < 6 else 2 if level == 6 else 3
    cmf = 0x78
    flg = flevel << 6
    flg += 31 - ((cmf << 8) + flg) % 31
    return bytes([cmf, flg])


def band_rows_for(stride: int, height: int) -> int:
    return max(1, min(height, (1 << 22) // stride))


@dataclass
class EncodedStream:
    """Where each row-band's compressed bytes live inside a PNG eXact wrote.

    After a successful save this describes the destination file, which then serves as an
    on-disk cache: bands whose rows have not been touched since are copied from it
    instead of being re-filtered and re-compressed.
    """

    shape: tuple[int, int, int]
    level: int
    band_rows: int
    first_idat: int  # file offset of the first IDAT chunk header
    bands: list[tuple[int, int, int, int]]  # (stream offset, length, adler32, raw length)
    path: str = ""
    stat_key: tuple[int, int, int] | None = None
    reused_bands: int = 0

    def bind(self, path: str) -> None:
        st = os.stat(path)
        self.path = path
        self.stat_key = (st.st_ino, st.st_size, st.st_mtime_ns)

    def usable_for(self, shape: tuple[int, ...], level: int) -> bool:
        if not self.path or self.shape != tuple(shape) or self.level != level:
            return False
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        return (st.st_ino, st.st_size, st.st_mtime_ns) == self.stat_key

    def read_band(self, fh: BinaryIO, index: int) -> bytes:
        offset, length, _, _ = self.bands[index]
        out = bytearray()
        while length:
            chunk, within = divmod(offset, IDAT_CHUNK_SIZE)
            fh.seek(self.first_idat + chunk * (IDAT_CHUNK_SIZE + 12) + 8 + within)
            n = min(length, IDAT_CHUNK_SIZE - within)
            piece = fh.read(n)
            if len(piece) != n:
                raise OSError("cached PNG is shorter than recorded")
            out += piece
            offset += n
            length -= n
        return bytes(out)


def encode_png(
    fp: BinaryIO,
    pixels: np.ndarray,
    plan: SavePlan,
    *,
    compress_level: int = DEFAULT_COMPRESS_LEVEL,
    reuse: EncodedStream | None = None,
    dirty_rows: np.ndarray | None = None,
) -> EncodedStream:
    """Write a complete PNG for *pixels* using the chunk layout in *plan*.

    With *reuse* (a previous encode of a same-shaped buffer) and *dirty_rows* (bool per
    row, True where pixels may have changed since), clean bands are copied from the
    previous file. Callers must verify the result; save_png_verified does.
    """
    mode = validate_buffer(pixels)
    if mode != plan.mode:
        raise ValueError(f"buffer mode {mode} does not match plan mode {plan.mode}")
    height, width, channels = pixels.shape
    stride = width * channels
    rows = pixels.reshape(height, stride)
    band_rows = band_rows_for(stride, height)
    starts = list(range(0, height, band_rows))
    zero_row = np.zeros(stride, dtype=np.uint8)
    if reuse is not None and (dirty_rows is None or not reuse.usable_for(pixels.shape, compress_level)
                              or reuse.band_rows != band_rows):
        reuse = None

    def band_is_clean(i: int) -> bool:
        y0 = starts[i]
        return reuse is not None and not dirty_rows[max(0, y0 - 1):y0 + band_rows].any()

    fp.write(PNG_SIGNATURE)
    write_chunk(fp, b"IHDR", plan.ihdr.to_bytes())
    for chunk in plan.before_idat:
        write_chunk(fp, chunk.type, chunk.data)
    first_idat = fp.tell()

    pending = bytearray(_zlib_header(compress_level))
    stream_offset = len(pending)
    adler = 1
    bands: list[tuple[int, int, int, int]] = []
    reused = 0

    def flush_full_chunks() -> None:
        while len(pending) >= IDAT_CHUNK_SIZE:
            write_chunk(fp, b"IDAT", bytes(pending[:IDAT_CHUNK_SIZE]))
            del pending[:IDAT_CHUNK_SIZE]

    def compute(i: int):
        y0 = starts[i]
        prev = rows[y0 - 1] if y0 else zero_row
        return _deflate_band(rows[y0:y0 + band_rows], prev, channels, compress_level)

    workers = max(1, min(8, os.cpu_count() or 1))
    cache_fh = open(reuse.path, "rb") if reuse is not None else None
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="exact-png") as pool:
            queue: deque = deque()
            next_band = 0
            while next_band < len(starts) or queue:
                while next_band < len(starts) and len(queue) < 2 * workers:
                    queue.append(None if band_is_clean(next_band) else pool.submit(compute, next_band))
                    next_band += 1
                index = next_band - len(queue)
                item = queue.popleft()
                if item is None:
                    _, _, band_adler, raw_len = reuse.bands[index]
                    data = reuse.read_band(cache_fh, index)
                    reused += 1
                else:
                    data, band_adler, raw_len = item.result()
                bands.append((stream_offset, len(data), band_adler, raw_len))
                stream_offset += len(data)
                adler = adler32_combine(adler, band_adler, raw_len)
                pending += data
                flush_full_chunks()
    finally:
        if cache_fh is not None:
            cache_fh.close()
    pending += FINAL_EMPTY_DEFLATE_BLOCK + struct.pack(">I", adler)
    flush_full_chunks()
    if pending:
        write_chunk(fp, b"IDAT", bytes(pending))

    for chunk in plan.after_idat:
        write_chunk(fp, chunk.type, chunk.data)
    write_chunk(fp, b"IEND", b"")
    return EncodedStream(tuple(pixels.shape), compress_level, band_rows, first_idat, bands, reused_bands=reused)


def verify_png_file(path: str, expected: np.ndarray, plan: SavePlan | None = None) -> tuple[str, ChunkLayout]:
    """Re-open *path* and confirm it decodes to exactly *expected*.

    Returns the SHA-256 of the decoded canonical bytes and the file's chunk layout. Raises SaveVerificationError on
    any mismatch (structure, mode, dimensions or a single channel byte).
    """
    mode = validate_buffer(expected)
    height, width, channels = expected.shape
    try:
        layout = read_chunk_layout(path)
    except PngFormatError as exc:
        raise SaveVerificationError(f"written file is not a valid PNG: {exc}") from exc
    if layout.crc_errors:
        raise SaveVerificationError(f"written file has CRC errors in {layout.crc_errors}")
    ihdr = layout.ihdr
    if (ihdr.width, ihdr.height, ihdr.bit_depth, ihdr.color_type) != (width, height, 8, COLOR_TYPE_FOR_MODE[mode]):
        raise SaveVerificationError(f"written IHDR {ihdr} does not match the intended {width}x{height} 8-bit {mode}")
    if plan is not None:
        if [(c.type, c.data) for c in layout.before_idat] != [(c.type, c.data) for c in plan.before_idat] or \
           [(c.type, c.data) for c in layout.after_idat] != [(c.type, c.data) for c in plan.after_idat]:
            raise SaveVerificationError("written ancillary chunks differ from the save plan")

    sha = hashlib.sha256()
    try:
        im = Image.open(path)
        im.load()
    except (OSError, ValueError, SyntaxError, zlib.error) as exc:
        raise SaveVerificationError(f"written file could not be decoded: {exc}") from exc
    with im:
        if im.mode != mode or im.size != (width, height):
            raise SaveVerificationError(f"decoder reports {im.mode} {im.size}, expected {mode} {(width, height)}")
        for y0 in range(0, height, STRIP_ROWS):
            y1 = min(height, y0 + STRIP_ROWS)
            decoded = im.crop((0, y0, width, y1)).tobytes()
            intended = expected[y0:y1]
            if decoded != memoryview(intended).cast("B"):
                got = np.frombuffer(decoded, dtype=np.uint8).reshape(intended.shape)
                ys, xs = np.nonzero((got != intended).any(axis=2))
                y, x = int(ys[0]) + y0, int(xs[0])
                raise SaveVerificationError(
                    f"decoded pixel ({x}, {y}) is {tuple(int(v) for v in got[y - y0, x])}, "
                    f"expected {tuple(int(v) for v in intended[y - y0, x])}"
                )
            sha.update(decoded)
    return sha.hexdigest(), layout


@dataclass
class SaveReport:
    dest: str
    file_size: int
    file_sha256: str
    pixel_sha256: str
    decisions: list[ChunkDecision]
    elapsed: float
    encoded: EncodedStream | None = None
    bands_reused: int = 0
    bands_total: int = 0


def save_png_verified(
    pixels: np.ndarray,
    plan: SavePlan,
    *,
    compress_level: int = DEFAULT_COMPRESS_LEVEL,
    reuse: EncodedStream | None = None,
    dirty_rows: np.ndarray | None = None,
) -> SaveReport:
    """Encode to a temp file beside the destination, verify, then atomically replace.

    If anything fails (encoding, I/O, verification) the destination is left untouched and
    the temporary file is removed. If reused bands fail verification (e.g. the cache file
    was altered in a way its stat did not reveal), the save is retried with a full encode.
    """
    try:
        return _save_once(pixels, plan, compress_level, reuse, dirty_rows)
    except SaveVerificationError:
        if reuse is None:
            raise
        return _save_once(pixels, plan, compress_level, None, None)


def _save_once(pixels, plan, compress_level, reuse, dirty_rows) -> SaveReport:
    started = time.perf_counter()
    validate_buffer(pixels)
    dest = os.path.realpath(plan.dest)
    directory = os.path.dirname(dest) or "."
    intended_sha = buffer_sha256(pixels)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(dest) + ".", suffix=".exact-tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fp:
            encoded = encode_png(fp, pixels, plan, compress_level=compress_level, reuse=reuse, dirty_rows=dirty_rows)
            fp.flush()
            os.fsync(fp.fileno())
        decoded_sha, layout = verify_png_file(tmp, pixels, plan)
        if decoded_sha != intended_sha:  # belt and braces: byte compare already passed
            raise SaveVerificationError("decoded SHA-256 does not match the intended buffer")
        if os.path.exists(dest):
            os.chmod(tmp, stat.S_IMODE(os.stat(dest).st_mode))
        else:
            os.chmod(tmp, 0o666 & ~_UMASK)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(directory)
    encoded.bind(dest)
    return SaveReport(
        dest=dest,
        file_size=layout.file_size,
        file_sha256=layout.file_sha256,
        pixel_sha256=decoded_sha,
        decisions=plan.decisions,
        elapsed=time.perf_counter() - started,
        encoded=encoded,
        bands_reused=encoded.reused_bands,
        bands_total=len(encoded.bands),
    )


def _fsync_directory(directory: str) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def save_png(path: str, pixels: np.ndarray, *, compress_level: int = DEFAULT_COMPRESS_LEVEL) -> SaveReport:
    """Convenience: verified save of a buffer with no source metadata."""
    plan = plan_save(path, pixels, None, pixels_changed=True, transposed=False)
    return save_png_verified(pixels, plan, compress_level=compress_level)
