#!/usr/bin/env python3
"""ImageStore - stash any file, folder or string inside an RGBA PNG, using all four channels (alpha too).

    imagestore.py                      interactive menu (in a terminal)
    imagestore.py -g | --gui           graphical interface (optional, needs PyQt6)
    imagestore.py encode FILE_OR_FOLDER [-o OUT] [--level fast|balanced|max] [--no-compress] [-f] [--no-verify]
    imagestore.py encode -t "some text" [-o OUT]
    imagestore.py decode IMAGE [-o DIR] [-f]
    imagestore.py info IMAGE
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import functools
import hmac
import io
import itertools
import math
import os
import queue
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import threading
import zlib
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

try:
    import lzma
except ImportError:  # a Python built without the xz headers has no lzma module
    lzma = None
try:
    from compression import zstd  # Python 3.14+
except ImportError:
    try:
        from backports import zstd  # pip install backports.zstd: the same module for older Pythons
    except ImportError:
        zstd = None
try:
    import numpy as np
except ImportError:  # optional: error correction is several times faster with it
    np = None
from PIL import Image
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

HAVE_LZMA = lzma is not None
HAVE_ZSTD = zstd is not None
if os.environ.get("IMAGESTORE_NO_NUMPY"):
    np = None  # force the pure Python paths (used by the tests and for comparing speed)
MAGIC = b"IMST"
VERSION = 2  # v2: SHA-256 digest replaced the v1 CRC32
FLAG_ZLIB = 0x01
FLAG_TEXT = 0x02  # payload is UTF-8 text: decode prints it instead of writing a file
FLAG_DIR = 0x04   # payload is an uncompressed tar of a folder
FLAG_SQUEEZE = 0x08  # payload has its zero runs squeezed out (folders only)
FLAG_LZMA = 0x10  # payload is a raw LZMA2 stream
FLAG_ECC = 0x20  # the pixels are interleaved Reed-Solomon codewords (see the error correction section)
FLAG_ZSTD = 0x40  # payload is a zstd frame
FLAGS_PACKED = FLAG_ZLIB | FLAG_LZMA | FLAG_SQUEEZE | FLAG_ZSTD  # at most one of these is set
# magic, version, flags, original size, stored size, sha256 of original, name length
HEADER = struct.Struct(">4sBBQQ32sH")
TEXT_NAME = "text.txt"
# matched against entry names (never the root folder itself) when auto-ignore is on
IGNORE_PATTERNS = (".DS_Store", "._*", "Thumbs.db", "desktop.ini", ".Trashes", ".Spotlight-V100",
                   "node_modules", "__pycache__", "*.pyc", ".git", ".venv", "venv")
LZMA_WINDOW = 64 << 20  # default cap on the compression window (zstd and LZMA)
LZMA_WINDOW_LIMIT = 1 << 30  # the most a user may ask for
# --level trades speed for size. fast and balanced use zstd, max uses LZMA; a Python without
# one of them falls back to the other, and to zlib without either.
LEVELS = ("fast", "balanced", "max")
DEFAULT_LEVEL = "balanced"
ZSTD_LEVELS = {"fast": 3, "balanced": 9, "max": 19}
LZMA_PRESETS = {"fast": 1, "balanced": 4, "max": 6}
WORKERS = (getattr(os, "process_cpu_count", None) or os.cpu_count)() or 1
SQUEEZE_MIN = 8  # shortest run of zero bytes worth a record of its own
_ZERO_RUN = re.compile(rb"\x00{%d,}" % SQUEEZE_MIN)
BPP = 4  # R, G, B, A: every byte of every channel carries data
CHUNK = 1 << 20  # streaming granularity: hashing, (de)compression and pixel reads work in pieces this size
SPOOL = 8 << 20  # scratch data stays in RAM up to this size, then spills to a temp file

console = Console()


class ImageStoreError(Exception):
    """A user-facing failure. ``kind`` names the failure class so frontends can explain it
    (see ``explain``); the message itself stays the precise technical diagnosis."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


class Cancelled(ImageStoreError):
    def __init__(self, message: str = "cancelled; nothing was written"):
        super().__init__(message, "cancelled")


@dataclass
class EccReport:
    """Error correction in an image: its strength, and what decoding had to repair."""
    parity: int  # parity bytes per 255-byte block
    blocks: int
    repaired_bytes: int = 0
    repaired_blocks: int = 0
    failed_blocks: int = 0  # damaged beyond repair

    @property
    def overhead(self) -> float:
        return self.parity / ECC_BLOCK

    @property
    def tolerance(self) -> float:
        """Share of the image that can be damaged and still be repaired."""
        return self.parity // 2 / ECC_BLOCK


@dataclass
class Meta:
    name: str
    flags: int
    orig_size: int
    stored_size: int
    digest: bytes
    ecc: EccReport | None = None  # not part of the header: set from the pixels while encoding/decoding

    @property
    def compressed(self) -> bool:
        return bool(self.flags & (FLAG_ZLIB | FLAG_LZMA | FLAG_ZSTD))

    @property
    def is_text(self) -> bool:
        return bool(self.flags & FLAG_TEXT)

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & FLAG_DIR)

    @property
    def squeezed(self) -> bool:
        return bool(self.flags & FLAG_SQUEEZE)

    @property
    def method(self) -> str:
        """How the payload is stored: zlib, lzma, zstd, squeezed or raw."""
        return next((n for f, n in ((FLAG_ZLIB, "zlib"), (FLAG_LZMA, "lzma"), (FLAG_ZSTD, "zstd"),
                                    (FLAG_SQUEEZE, "squeezed"))
                     if self.flags & f), "raw")


# --------------------------------------------------------------------------- core


def _spool() -> BinaryIO:
    return tempfile.SpooledTemporaryFile(max_size=SPOOL)


class _Sha256Behind:
    """SHA-256 on a helper thread, so hashing overlaps the (de)compression on the calling one
    (hashlib lets go of the GIL for large buffers). update() takes chunks that are never changed
    afterwards; finish() returns the digest and must always be called, to end the thread."""

    def __init__(self) -> None:
        self.sha = hashlib.sha256()
        self.queue: queue.Queue = queue.Queue(8)
        self.thread = threading.Thread(target=self._run, name="imagestore-sha256", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while (chunk := self.queue.get()) is not None:
            self.sha.update(chunk)

    def update(self, chunk) -> None:
        self.queue.put(chunk)

    def finish(self) -> bytes:
        if self.thread.is_alive():
            self.queue.put(None)
            self.thread.join()
        return self.sha.digest()


def lzma_window(orig_size: int, cap: int = LZMA_WINDOW) -> int:
    """The window used for *orig_size* bytes: the smallest power of two that covers the data,
    but at most *cap* (itself rounded up to a power of two and held within 4 KiB .. the limit)."""
    cap = 1 << (min(max(cap, 4096), LZMA_WINDOW_LIMIT) - 1).bit_length()
    return min(1 << max(12, max(orig_size - 1, 0).bit_length()), cap)


def lzma_filters(window: int, preset: int = 6) -> list[dict]:
    return [{"id": lzma.FILTER_LZMA2, "preset": preset, "dict_size": window}]


def compressor_for(level: str) -> str:
    """The method *level* compresses with in this Python: zstd, lzma or zlib."""
    if level == "max" and lzma:
        return "lzma"
    if zstd:
        return "zstd"
    return "lzma" if lzma else "zlib"


def fallback_note(level: str) -> str | None:
    """Why *level* compresses with something other than its own method in this Python, if it does."""
    wanted, got = ("lzma" if level == "max" else "zstd"), compressor_for(level)
    if wanted == got:
        return None
    need = "Python 3.14+ or pip install backports.zstd" if wanted == "zstd" else "a Python built with lzma"
    return f"{wanted} is not available here ({need}), so {got} was used"


def _memory_budget() -> int:
    """Half the physical memory, for sizing parallel work (2 GiB when it cannot be found)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2
    except (AttributeError, ValueError, OSError):
        return 2 << 30


class _Compressor:
    """Compresses a stream into scratch space on every core: write() chunks in order, then finish().

    zstd runs its own worker threads. LZMA is cut into window-sized blocks compressed in
    parallel; every LZMA2 stream opens with a dictionary reset, so the blocks joined without
    their end markers (plus one at the very end) are a single stream that any LZMA2 decoder,
    including older ImageStore versions, reads as usual. zlib, used only when neither is
    built in, runs on one core."""

    def __init__(self, method: str, level: str, total: int, window: int):
        self.method, self.spool, self.size = method, _spool(), 0
        self.win = lzma_window(total, window)
        self.pool: ThreadPoolExecutor | None = None
        if method == "zstd":
            P = zstd.CompressionParameter
            self.flag = FLAG_ZSTD
            self.co = zstd.ZstdCompressor(options={
                P.compression_level: ZSTD_LEVELS[level], P.window_log: self.win.bit_length() - 1,
                P.enable_long_distance_matching: 1,  # finds repeats a whole window apart: duplicate files
                P.nb_workers: min(WORKERS, P.nb_workers.bounds()[1]) if total > CHUNK else 0})
        elif method == "lzma":
            self.flag = FLAG_LZMA
            self.filters = lzma_filters(self.win, LZMA_PRESETS[level])
            self.size = self.spool.write(bytes([self.win.bit_length() - 1]))
            # an encoder needs about 11x its window, so the thread count follows the memory
            self.threads = max(1, min(WORKERS, -(-total // self.win), _memory_budget() // (12 * self.win)))
            self.block, self.pending = bytearray(), deque()
            if self.threads > 1:
                self.pool = ThreadPoolExecutor(self.threads, thread_name_prefix="imagestore-lzma")
        else:
            self.flag = FLAG_ZLIB
            self.co = zlib.compressobj(9 if level == "max" else 6)

    def _out(self, data: bytes) -> None:
        self.size += self.spool.write(data)

    def _lzma(self, block) -> bytes:
        out = lzma.compress(block, lzma.FORMAT_RAW, filters=self.filters)
        return out[:-1]  # the end marker (a 0x00 control byte) goes on once, after the last block

    def _submit(self, block) -> None:
        if self.pool is None:
            self._out(self._lzma(block))
            return
        self.pending.append(self.pool.submit(self._lzma, block))
        while len(self.pending) > self.threads:  # bounded: no more blocks in memory than threads
            self._out(self.pending.popleft().result())

    def write(self, chunk) -> None:
        if self.method != "lzma":
            self._out(self.co.compress(chunk))
            return
        self.block += chunk
        while len(self.block) >= self.win:
            block = self.block[:self.win]
            del self.block[:self.win]
            self._submit(block)

    def finish(self) -> None:
        if self.method != "lzma":
            self._out(self.co.flush())
            self.co = None  # a multithreaded zstd context keeps hundreds of MiB until it is freed
            return
        if self.block:
            self._submit(self.block)
            self.block = bytearray()
        while self.pending:
            self._out(self.pending.popleft().result())
        self._out(b"\x00")
        self._shutdown()

    def _shutdown(self) -> None:
        if self.pool:
            for job in self.pending:  # after a failure or cancel: drop what has not started
                job.cancel()
            self.pool.shutdown(wait=True)
            self.pool = None

    def close(self) -> None:
        self.co = None
        self._shutdown()
        self.spool.close()


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def squeeze(chunk: bytes) -> bytes:
    """Replace zero runs with their length: records of varint(zeros) varint(len) literal.
    Chunks are squeezed independently, so a run across a boundary becomes two records."""
    out, zeros, pos = [], 0, 0
    for m in _ZERO_RUN.finditer(chunk):
        lit = chunk[pos:m.start()]
        if zeros or lit:
            out += (_varint(zeros), _varint(len(lit)), lit)
        zeros, pos = m.end() - m.start(), m.end()
    lit = chunk[pos:]
    if zeros or lit:
        out += (_varint(zeros), _varint(len(lit)), lit)
    return b"".join(out)


def encode(src: BinaryIO, name: str, compress: bool = True, kind: int = 0,
           window: int = LZMA_WINDOW, ecc: int = 0, task: Task | None = None,
           level: str = DEFAULT_LEVEL) -> tuple[Image.Image, Meta]:
    """Build the image from a seekable binary stream, holding only the pixel buffer in memory.
    level picks the compressor (see LEVELS) and window caps its window in bytes. An LZMA
    payload starts with one byte, log2 of the window used, because a raw LZMA stream does not
    record it. ecc is the parity bytes per 255-byte block (0 = none; see ecc_parity_bytes)."""
    buf, size, meta = encode_pixels(src, name, compress, kind, window, ecc, task, level)
    # RGBA maps the buffer in place instead of copying it
    return Image.frombuffer("RGBA", size, buf, "raw", "RGBA", 0, 1), meta


def encode_pixels(src: BinaryIO, name: str, compress: bool = True, kind: int = 0,
                  window: int = LZMA_WINDOW, ecc: int = 0, task: Task | None = None,
                  level: str = DEFAULT_LEVEL) -> tuple[bytearray, tuple[int, int], Meta]:
    """encode(), returning the raw RGBA bytes and (width, height) instead of an Image."""
    total = src.seek(0, os.SEEK_END)
    src.seek(0)
    return _pack_payload(iter(lambda: src.read(CHUNK), b""), total, src, name, compress, kind, window, ecc, task,
                         level)


class _RawWins(Exception):
    """The payload is smallest stored as is, but the stream it came from cannot be read again."""


def _pack_payload(chunks: Iterator[bytes], total: int, raw: BinaryIO | None, name: str, compress: bool, kind: int,
                  window: int, ecc: int, task: Task | None, level: str) -> tuple[bytearray, tuple[int, int], Meta]:
    """Hash and compress chunks (about total bytes; never more than it is, as it sizes the window)
    and lay out the result. raw is the same data as a seekable stream, used when storing it as is
    wins; without one, that case raises _RawWins."""
    sha, size = _Sha256Behind(), 0
    packer = squeezed = None
    squeezed_size = 0
    try:
        packer = _Compressor(compressor_for(level), level, total, window) if compress else None
        squeezed = _spool() if kind & FLAG_DIR and not compress else None  # tar padding is zeros
        try:
            for chunk in chunks:
                sha.update(chunk)
                size += len(chunk)
                if packer:
                    packer.write(chunk)
                if squeezed:
                    squeezed_size += squeezed.write(squeeze(chunk))
            if packer:
                packer.finish()
        except MemoryError as e:
            raise ImageStoreError("not enough memory to " + (
                f"compress with a {packer.win >> 20} MiB window" if packer else "encode this"), "memory") from e
        flags, payload, stored = kind, raw, size
        for flag, spool, n in ((packer.flag, packer.spool, packer.size) if packer else (0, None, 0),
                               (FLAG_SQUEEZE, squeezed, squeezed_size)):
            if spool and n < stored:
                flags, payload, stored = kind | flag, spool, n
        if payload is None:
            raise _RawWins()
        return _layout(payload, name, flags, size, stored, sha.finish(), ecc, task)
    finally:
        sha.finish()
        if packer:
            packer.close()
        if squeezed:
            squeezed.close()


def _layout(payload: BinaryIO, name: str, flags: int, size: int, stored: int, digest: bytes, ecc: int,
            task: Task | None) -> tuple[bytearray, tuple[int, int], Meta]:
    """The header and *stored* bytes of payload as pixels (with parity when ecc is set)."""
    if ecc:
        flags |= FLAG_ECC
    name_b = name.encode("utf-8")
    meta = Meta(name, flags, size, stored, digest)
    head = HEADER.pack(MAGIC, VERSION, flags, size, stored, meta.digest, len(name_b)) + name_b

    used = len(head) + stored
    if ecc:  # whole blocks of (255 - ecc) data bytes, and at least ECC_MIN_BLOCKS of them
        data_bytes = ECC_BLOCK - ecc
        pixels = math.ceil(max(ECC_MIN_BLOCKS, -(-used // data_bytes)) * ECC_BLOCK / BPP)
    else:
        pixels = math.ceil(used / BPP)
    width = math.ceil(math.sqrt(pixels))
    height = math.ceil(pixels / width)
    blocks = width * height * BPP // ECC_BLOCK
    # zero-filled, so the padding needs no extra pass
    buf = bytearray(blocks * (ECC_BLOCK - ecc) if ecc else width * height * BPP)
    buf[:len(head)] = head
    view, pos, end = memoryview(buf), len(head), used
    payload.seek(0)
    while pos < end:
        n = payload.readinto(view[pos:end])
        if not n:
            raise ImageStoreError("source changed size while it was being encoded", "source_changed")
        pos += n
    if ecc:
        buf = _ecc_encode(buf, used, blocks, ecc, width * height * BPP, task)
        meta.ecc = EccReport(ecc, blocks)
    return buf, (width, height), meta


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_BAND = 4 << 20  # rows are deflated in bands of about this many bytes, one band per task


def _png_chunk(f: BinaryIO, kind: bytes, data) -> None:
    f.write(struct.pack(">I", len(data)) + kind)
    f.write(data)
    f.write(struct.pack(">I", zlib.crc32(data, zlib.crc32(kind))))


def write_png(path: Path, buf, width: int, height: int, task: Task | None = None, level: int = 1) -> None:
    """Write RGBA pixels as an 8-bit PNG, deflating bands of rows on every core (as pigz does).

    Each band is its own raw deflate stream ended by a sync flush, which leaves it byte-aligned
    and not final, so the bands joined behind one zlib header (with the Adler-32 of all of
    them) are a single valid zlib stream. Each band becomes one IDAT chunk. Filter None and
    level 1 at most: payload pixels look like noise, so neither a filter nor more effort would
    pay off, and level 0 (stored, at copying speed) is right for data that is already compressed."""
    stride = width * BPP
    rows = max(1, PNG_BAND // stride)
    view = memoryview(buf)

    def band(y0: int) -> tuple[bytes, int, int]:  # (deflated, raw length, Adler-32 of raw)
        y1 = min(y0 + rows, height)
        raw = b"".join(itertools.chain.from_iterable(
            (b"\0", view[y * stride:(y + 1) * stride]) for y in range(y0, y1)))  # filter type 0 per row
        co = zlib.compressobj(level, zlib.DEFLATED, -15)
        return co.compress(raw) + co.flush(zlib.Z_FINISH if y1 == height else zlib.Z_SYNC_FLUSH), len(raw), \
            zlib.adler32(raw)

    with open(path, "wb") as f:
        f.write(PNG_SIGNATURE)
        _png_chunk(f, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))  # 8-bit RGBA
        _png_chunk(f, b"IDAT", b"\x78\x01")  # zlib header: deflate, 32 KiB window, fastest (FLEVEL is a hint)
        adler = 1
        with ThreadPoolExecutor(WORKERS, thread_name_prefix="imagestore-png") as pool:
            pending: deque = deque()
            for y in range(0, height, rows):
                pending.append(pool.submit(band, y))
                while len(pending) > 2 * WORKERS or (y + rows >= height and pending):
                    data, n, part = pending.popleft().result()
                    adler = _adler32_combine(adler, part, n)
                    _png_chunk(f, b"IDAT", data)
                    if task:
                        task.advance(n)
        _png_chunk(f, b"IDAT", struct.pack(">I", adler))
        _png_chunk(f, b"IEND", b"")


def _adler32_combine(a: int, b: int, len_b: int) -> int:
    """The Adler-32 of two pieces back to back, from each one's own (zlib's adler32_combine)."""
    base = 65521
    rem = len_b % base
    lo = (a & 0xFFFF) + (b & 0xFFFF) + base - 1
    hi = (rem * (a & 0xFFFF)) % base + (a >> 16) + (b >> 16) + base - rem
    return (lo % base) | (hi % base) << 16


class _Pixels:
    """Sequential reader over an image's raw RGBA bytes, fetched a band of rows at a time so the
    whole image is never duplicated as one bytes object."""

    def __init__(self, img: Image.Image):
        if img.mode != "RGBA":
            raise ImageStoreError(f"image mode is {img.mode}, expected RGBA (was the alpha channel stripped?)", "mode")
        self.img = img
        self.total = img.width * img.height * BPP
        self._bands = self._iter_bands()
        self._buf = memoryview(b"")

    def _iter_bands(self) -> Iterator[bytes]:
        w, h = self.img.size
        rows = max(1, CHUNK // (w * BPP))
        for y in range(0, h, rows):
            yield self.img.crop((0, y, w, min(y + rows, h))).tobytes()

    def read(self, n: int) -> memoryview | bytes:
        """Up to n bytes; fewer only at the end of the image."""
        parts, got = [], 0
        while got < n:
            if not self._buf:
                self._buf = memoryview(next(self._bands, b""))
                if not self._buf:
                    break
            part = self._buf[:n - got]
            self._buf = self._buf[len(part):]
            parts.append(part)
            got += len(part)
        return parts[0] if len(parts) == 1 else b"".join(parts)


def _payload(px: _Pixels, stored: int) -> Iterator[bytes]:
    left = stored
    while left:
        chunk = px.read(min(left, CHUNK))
        if not chunk:
            raise ImageStoreError("image is truncated (payload extends past the pixel data)", "truncated")
        left -= len(chunk)
        yield chunk


def _inflate(chunks: Iterator[bytes], orig_size: int) -> Iterator[bytes]:
    # output is capped at the declared size so a bogus header can't balloon memory
    d, left = zlib.decompressobj(), orig_size
    try:
        for chunk in chunks:
            while chunk and left and not d.eof:
                out = d.decompress(chunk, min(left, CHUNK))
                rest = d.unconsumed_tail
                if not out and len(rest) == len(chunk):
                    break  # no progress: let the size check report it
                chunk, left = rest, left - len(out)
                if out:
                    yield out
    except zlib.error as e:
        raise ImageStoreError(f"corrupt compressed payload: {e}", "zlib") from e


def _read_varint(buf: bytearray, pos: int) -> tuple[int, int] | None:
    """(value, next position), or None while the number is still incomplete."""
    value = shift = 0
    for i in range(pos, min(len(buf), pos + 10)):
        value |= (buf[i] & 0x7F) << shift
        if not buf[i] & 0x80:
            return value, i + 1
        shift += 7
    if len(buf) - pos >= 10:
        raise ImageStoreError("corrupt squeezed payload: bad record length", "squeeze")
    return None


def _unlzma(chunks: Iterator[bytes], orig_size: int) -> Iterator[bytes]:
    if lzma is None:
        raise ImageStoreError("this image is LZMA-compressed, and this Python was built without lzma support",
                              "no_lzma")
    chunks = iter(chunks)
    first = next(chunks, b"")
    exp = first[0] if first else 0
    if not 12 <= exp <= 30:
        raise ImageStoreError("corrupt compressed payload: bad LZMA window", "zlib")
    # a window larger than the data is never used, so a hostile header can't force a huge allocation
    window = min(1 << exp, lzma_window(orig_size, LZMA_WINDOW_LIMIT))
    d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=lzma_filters(window))
    yield from _drain(d, itertools.chain([first[1:]], chunks), orig_size, lzma.LZMAError)


def _unzstd(chunks: Iterator[bytes], orig_size: int) -> Iterator[bytes]:
    if zstd is None:
        raise ImageStoreError("this image is zstd-compressed, which needs Python 3.14 or newer "
                              "(or: pip install backports.zstd)", "no_zstd")
    # as for LZMA, a hostile frame can't ask for a window larger than the data
    limit = lzma_window(orig_size, LZMA_WINDOW_LIMIT).bit_length() - 1
    d = zstd.ZstdDecompressor(options={zstd.DecompressionParameter.window_log_max: max(10, limit)})
    yield from _drain(d, chunks, orig_size, zstd.ZstdError)


def _drain(d, chunks: Iterator[bytes], orig_size: int, error: type[Exception]) -> Iterator[bytes]:
    """Run an LZMA or zstd decompressor over chunks, its output capped at the declared size."""
    left = orig_size
    try:
        for chunk in chunks:
            while left and not d.eof:
                out = d.decompress(chunk, min(left, CHUNK))
                chunk, left = b"", left - len(out)
                if out:
                    yield out
                if d.needs_input:
                    break
    except error as e:
        raise ImageStoreError(f"corrupt compressed payload: {e}", "zlib") from e


def _unsqueeze(chunks: Iterator[bytes], orig_size: int) -> Iterator[bytes]:
    """Inverse of squeeze. Output is capped at the declared size, like _inflate."""
    buf, left = bytearray(), orig_size
    zeros = lit = 0  # zero bytes still to write, literal bytes still to copy
    for chunk in chunks:
        buf += chunk
        pos = 0
        while left:
            if zeros:
                n = min(zeros, left, CHUNK)
                zeros, left = zeros - n, left - n
                yield bytes(n)
            elif lit:
                n = min(lit, left, len(buf) - pos)
                if not n:
                    break  # the rest of this literal is in the next chunk
                lit, left = lit - n, left - n
                yield bytes(buf[pos:pos + n])
                pos += n
            else:
                head = _read_varint(buf, pos)
                if head is None or (tail := _read_varint(buf, head[1])) is None:
                    break
                (zeros, _), (lit, pos) = head, tail
        del buf[:pos]
        if not left:
            return


def _verified(meta: Meta, chunks: Iterator[bytes]) -> Iterator[bytes]:
    sha, n = _Sha256Behind(), 0
    try:
        for chunk in chunks:
            sha.update(chunk)
            n += len(chunk)
            yield chunk
    finally:
        digest = sha.finish()
    if n != meta.orig_size:
        raise ImageStoreError(f"size mismatch: expected {meta.orig_size} bytes, got {n}", "size")
    if not hmac.compare_digest(digest, meta.digest):
        raise ImageStoreError("SHA-256 mismatch: pixel data was altered (lossy re-encode or alpha premultiplication?)",
                             "sha")


# ------------------------------------------------------------------ error correction
#
# Reed-Solomon over GF(256), 255-byte blocks (255 - parity data bytes, then parity). The blocks
# are interleaved across the whole image (byte j of block i sits at j * blocks + i), so a stripe,
# a painted-over area or a cut-off corner costs each block only a few bytes. The data is stored
# unchanged next to its parity; the parity level is found again from the pixels (ECC_STEP).

ECC_BLOCK = 255
ECC_STEP = 8  # parity comes in multiples of this: fewer than 8 and a wrong guess "decodes" anything
ECC_MAX_PARITY = 128
ECC_MIN_BLOCKS = 4
ECC_HINT_BYTES = 16 << 20  # images this big are slow enough without NumPy to be worth a tip

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]
del _x, _i


def _gmul(a: int, b: int) -> int:
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def ecc_parity_bytes(percent: float) -> int:
    """Parity bytes per block for an overhead of *percent* of the image (0 = no error correction)."""
    if percent <= 0:
        return 0
    return min(ECC_MAX_PARITY, max(ECC_STEP, ECC_STEP * round(ECC_BLOCK * percent / 100 / ECC_STEP)))


class _RS:
    """Systematic Reed-Solomon coder for one parity size (roots alpha^0 .. alpha^(parity-1))."""

    def __init__(self, parity: int):
        self.parity = parity
        gen = [1]
        for i in range(parity):  # gen *= (x + alpha^i)
            nxt = gen + [0]
            for j, c in enumerate(gen):
                nxt[j + 1] ^= _gmul(c, _EXP[i])
            gen = nxt
        self.gen = gen
        self.mask = (1 << 8 * parity) - 1
        self.shift = 8 * (parity - 1)

    @functools.cached_property
    def np_feedback(self):
        """feedback as a (256, parity) uint8 table, for the vectorised coder"""
        return _vmul(np.arange(256, dtype=np.uint8)[:, None], np.array(self.gen[1:], dtype=np.uint8)[None, :])

    @functools.cached_property
    def feedback(self) -> list[int]:
        """feedback[f] is f * gen (without the leading 1), packed into one integer"""
        if np is not None:
            return [int.from_bytes(row.tobytes(), "big") for row in self.np_feedback]
        return [int.from_bytes(bytes(_gmul(f, g) for g in self.gen[1:]), "big") for f in range(256)]

    @functools.cached_property
    def times_alpha(self) -> list[list[int]]:
        return [[_gmul(_EXP[j], v) for v in range(256)] for j in range(self.parity)]

    @functools.cached_property
    def np_times_alpha(self):
        return np.array(self.times_alpha, dtype=np.uint8)

    @functools.cached_property
    def np_columns(self):
        """What a byte value v at data position j adds to the parity: (data bytes, 256, parity / 8) of
        64-bit lanes. Parity is linear in the data, so the parity of a block is the XOR of one table
        row per byte. Position j's row is v times the parity of a block that is 1 at j and 0 elsewhere."""
        k = ECC_BLOCK - self.parity
        unit = _np_parity_lfsr(self, np.eye(k, dtype=np.uint8))  # row j: parity of the unit block j
        exp, log = _np_gf()
        values = np.arange(256, dtype=np.int32)
        tables = exp[log[values][None, :, None] + log[unit][:, None, :]]
        return np.ascontiguousarray(tables).view(np.uint64)

    def parity_of(self, data) -> int:
        """Remainder of data(x) * x^parity modulo the generator, as one big-endian integer."""
        reg, feedback, mask, shift = 0, self.feedback, self.mask, self.shift
        for d in data:
            reg = ((reg << 8) & mask) ^ feedback[d ^ (reg >> shift)]
        return reg

    def _syndromes(self, remainder: int) -> list[int]:
        rem = remainder.to_bytes(self.parity, "big")
        out = []
        for table in self.times_alpha:
            acc = 0
            for b in rem:
                acc = table[acc] ^ b
            out.append(acc)
        return out

    def repair(self, block: bytearray) -> bool:
        """Fix a 255-byte block in place. False if it has more errors than the code can correct."""
        k = ECC_BLOCK - self.parity
        remainder = self.parity_of(block[:k]) ^ int.from_bytes(block[k:], "big")
        if not remainder:
            return True
        syn = self._syndromes(remainder)
        # Berlekamp-Massey: the error locator polynomial, lowest degree first
        loc, prev, length, gap, prev_d = [1], [1], 0, 1, 1
        for n in range(self.parity):
            d = syn[n]
            for i in range(1, length + 1):
                d ^= _gmul(loc[i], syn[n - i]) if i < len(loc) else 0
            if not d:
                gap += 1
                continue
            scaled = [_gmul(c, _EXP[_LOG[d] - _LOG[prev_d] + 255]) for c in prev]
            new = loc + [0] * max(0, gap + len(scaled) - len(loc))
            for i, c in enumerate(scaled):
                new[i + gap] ^= c
            if 2 * length <= n:
                prev, prev_d, length, gap = loc, d, n + 1 - length, 1
            else:
                gap += 1
            loc = new
        if length > self.parity // 2:
            return False
        loc = loc[:length + 1]
        if len(loc) <= length:
            loc += [0] * (length + 1 - len(loc))
        # Chien search: block position m has locator alpha^(254 - m)
        roots = []
        for e in range(ECC_BLOCK):
            x = (ECC_BLOCK - e) % ECC_BLOCK
            v = 0
            for i, c in enumerate(loc):
                if c:
                    v ^= _EXP[(_LOG[c] + i * x) % 255]
            if not v:
                roots.append(e)
        if len(roots) != length:
            return False
        # Forney: error values from the evaluator polynomial
        omega = [0] * self.parity
        for i in range(self.parity):
            for j in range(min(i, length) + 1):
                omega[i] ^= _gmul(loc[j], syn[i - j])
        fixed = bytearray(block)
        for e in roots:
            x = (ECC_BLOCK - e) % ECC_BLOCK
            num = den = 0
            for i, c in enumerate(omega):
                if c:
                    num ^= _EXP[(_LOG[c] + i * x) % 255]
            for i in range(1, length + 1, 2):
                if loc[i]:
                    den ^= _EXP[(_LOG[loc[i]] + (i - 1) * x) % 255]
            if not den:
                return False
            if num:
                fixed[ECC_BLOCK - 1 - e] ^= _EXP[(_LOG[num] - _LOG[den] + e) % 255]
        if self.parity_of(fixed[:k]) != int.from_bytes(fixed[k:], "big"):
            return False  # "corrected" into a different, wrong word: more damage than the code can repair
        block[:] = fixed
        return True


@functools.lru_cache(maxsize=32)  # detection tries every size
def _rs(parity: int) -> _RS:
    return _RS(parity)


# The same coder over many blocks at once with NumPy: every step below is one array operation
# across all blocks, so the work that costs a Python loop per block above is done in C.

ECC_CHUNK = 1 << 15  # blocks per batch: bounds memory and gives progress


@functools.lru_cache(maxsize=1)
def _np_gf():
    """exp and log tables where log(0) is 512, past any real sum of logs (at most 508), and exp is zero
    from 512 on: multiplying by zero then needs no special case."""
    exp = np.zeros(1536, np.uint8)
    exp[:510] = _EXP[:255] * 2
    log = np.array(_LOG, dtype=np.int32)
    log[0] = 512
    return exp, log


def _vmul(a, b):
    exp, log = _np_gf()
    return exp[log[a] + log[b]]


def _np_parity_lfsr(rs: _RS, rows):
    """Block-by-block shift register, one step per data byte (used to build the tables below).
    rows is (data bytes, blocks); returns (blocks, parity)."""
    table = rs.np_feedback
    reg = np.zeros((rows.shape[1], rs.parity), np.uint8)
    for row in rows:
        fb = row ^ reg[:, 0]
        reg[:, :-1] = reg[:, 1:]
        reg[:, -1] = 0
        reg ^= table[fb]
    return reg


def _np_parity(rs: _RS, rows):
    """Parity of every block at once. rows is (data bytes, blocks): row j holds byte j of each block.
    Returns (blocks, parity); XOR the stored parity into it to get the remainder (all zero when intact).
    One table lookup and one XOR per data byte, on 64-bit lanes (parity is a multiple of 8 bytes)."""
    tables = rs.np_columns
    acc = np.zeros((rows.shape[1], rs.parity // 8), np.uint64)
    for j in range(tables.shape[0]):
        acc ^= tables[j][rows[j]]
    return acc.view(np.uint8)


def _np_syndromes(rs: _RS, rem):
    """All syndromes of every remainder row, evaluated together (Horner, one step per byte)."""
    columns = np.arange(rs.parity)[None, :]
    acc = np.zeros(rem.shape, np.uint8)
    for b in range(rs.parity):
        acc = rs.np_times_alpha[columns, acc] ^ rem[:, b:b + 1]
    return acc


def _np_locator(syn):
    """Berlekamp-Massey for all blocks at once (the division-free form, so no per-block inversion).
    Returns the locator polynomials, lowest degree first, and their degrees. A block with more errors
    than the code can fix ends up with a degree past parity // 2, so the polynomials can be kept that
    narrow: whatever they hold for such a block is rejected by the caller."""
    count, parity = syn.shape
    width = parity // 2 + 2
    lam = np.zeros((count, width), np.uint8)
    lam[:, 0] = 1
    prev, gamma, deg = lam.copy(), np.ones(count, np.uint8), np.zeros(count, np.int64)
    for n in range(parity):
        m = min(n + 1, width)
        delta = np.bitwise_xor.reduce(_vmul(lam[:, :m], syn[:, n - m + 1:n + 1][:, ::-1]), axis=1)
        shifted = np.zeros_like(prev)
        shifted[:, 1:] = prev[:, :-1]
        new = _vmul(gamma[:, None], lam) ^ _vmul(delta[:, None], shifted)
        swap = (delta != 0) & (2 * deg <= n)
        prev = np.where(swap[:, None], lam, shifted)
        gamma = np.where(swap, delta, gamma)
        deg = np.where(swap, n + 1 - deg, deg)
        lam = new
    return lam, deg


def _np_eval(coeffs, exps, outer: bool = False):
    """XOR over i of coeffs[:, i] * alpha^exps[i], skipping zero coefficients. exps[i] pairs with the rows
    of coeffs one to one, or with outer=True is one vector of exponents applied to every row."""
    exp, log = _np_gf()
    out = 0
    for i in range(coeffs.shape[1]):
        c = coeffs[:, i]
        out = out ^ exp[(log[c][:, None] if outer else log[c]) + exps[i]]
    return out


def _np_repair(rs: _RS, blocks, rem):
    """Repair damaged blocks in place. blocks is (n, 255), rem the matching remainders.
    Returns which of them could be repaired; the others are left untouched."""
    exp, log = _np_gf()
    k, parity, count = ECC_BLOCK - rs.parity, rs.parity, len(blocks)
    syn = _np_syndromes(rs, rem)
    lam, deg = _np_locator(syn)
    ok = (deg > 0) & (deg <= parity // 2)
    if not ok.any():
        return ok
    top = int(deg[ok].max())
    # Chien search: position e of a block has locator alpha^e, so evaluate at alpha^-e for all e
    at = (ECC_BLOCK - np.arange(ECC_BLOCK)) % ECC_BLOCK
    powers = np.array([(i * at) % ECC_BLOCK for i in range(top + 1)])
    roots = _np_eval(lam[:, :top + 1], powers, outer=True) == 0
    ok &= roots.sum(axis=1) == deg
    # Forney: the error value at each root, from the evaluator polynomial
    omega = np.zeros((count, parity), np.uint8)
    for j in range(top + 1):
        omega[:, j:] ^= _vmul(lam[:, j:j + 1], syn[:, :parity - j])
    row, pos = np.nonzero(roots & ok[:, None])
    x = at[pos]
    num = _np_eval(omega[row], np.array([(i * x) % ECC_BLOCK for i in range(parity)]))
    odd = lam[row][:, 1:top + 1:2]
    den = _np_eval(odd, np.array([((2 * i) * x) % ECC_BLOCK for i in range(odd.shape[1])]))
    ok[row[den == 0]] = False
    fix = np.where((num == 0) | (den == 0), np.uint8(0), exp[(log[num] - log[den] + pos) % ECC_BLOCK])
    fixed = blocks.copy()
    fixed[row, ECC_BLOCK - 1 - pos] ^= fix
    ok &= ~(_np_parity(rs, fixed[:, :k].T) ^ fixed[:, k:]).any(axis=1)
    blocks[ok] = fixed[ok]
    return ok




def _ecc_encode(data: bytearray, used: int, blocks: int, parity: int, size: int, task: Task | None) -> bytearray:
    """Lay *data* (blocks * (255 - parity) bytes, the last of it padding) out as interleaved codewords."""
    rs, k = _rs(parity), ECC_BLOCK - parity
    par = bytearray(blocks * parity)  # blocks past the data are all zero and so have zero parity
    view = memoryview(data)
    full = -(-used // k)
    if task:
        task.phase("Adding error correction", full)
    if np is not None:
        rows = np.frombuffer(data, np.uint8)[:full * k].reshape(full, k)
        for a in range(0, full, ECC_CHUNK):
            b = min(a + ECC_CHUNK, full)
            par[a * parity:b * parity] = _np_parity(rs, rows[a:b].T).tobytes()
            if task:
                task.advance(b - a)
    else:
        for i in range(full):
            par[i * parity:(i + 1) * parity] = rs.parity_of(view[i * k:(i + 1) * k]).to_bytes(parity, "big")
            if task and i % 256 == 255:
                task.advance(256)
    out = bytearray(size)
    for j in range(k):
        out[j * blocks:(j + 1) * blocks] = data[j::k]
    for j in range(parity):
        out[(k + j) * blocks:(k + j + 1) * blocks] = par[j::parity]
    return out


def _ecc_detect(buf: bytearray, blocks: int) -> int | None:
    """The parity size this image was made with, found by trying each on a few blocks.
    A block repairs under the right size and (almost never) under a wrong one, so this works
    even when every block is damaged."""
    picks = [b for b in (bytearray(buf[i:i + ECC_BLOCK * blocks:blocks])
                         for i in sorted({t * blocks // 8 for t in range(8)})) if any(b)]
    sizes = range(ECC_STEP, ECC_MAX_PARITY + 1, ECC_STEP)
    for parity in reversed(sizes):  # an undamaged image is recognised without trying any repairs
        # (a codeword of a stronger code also checks out under every weaker one, so take the largest)
        rs, k = _rs(parity), ECC_BLOCK - parity
        if picks and all(rs.parity_of(b[:k]) == int.from_bytes(b[k:], "big") for b in picks):
            return parity
    best, best_score = None, 0
    for parity in sizes:
        rs = _rs(parity)
        score = sum(rs.repair(bytearray(b)) for b in picks)
        if score >= best_score and score * 3 >= len(picks) * 2:
            best, best_score = parity, score
    return best


def _ecc_repair_np(rs: _RS, buf: bytearray, blocks: int, report: EccReport, task: Task | None) -> None:
    """Check every block and repair the damaged ones, a batch at a time, writing the fixes into buf."""
    k = ECC_BLOCK - rs.parity
    grid = np.frombuffer(buf, np.uint8)[:ECC_BLOCK * blocks].reshape(ECC_BLOCK, blocks)  # column i = block i
    for a in range(0, blocks, ECC_CHUNK):
        b = min(a + ECC_CHUNK, blocks)
        part = grid[:, a:b]
        rem = _np_parity(rs, part[:k]) ^ part[k:].T
        bad = np.nonzero(rem.any(axis=1))[0]
        if bad.size:
            damaged = np.ascontiguousarray(part[:, bad].T)
            before = damaged.copy()
            ok = _np_repair(rs, damaged, rem[bad])
            report.failed_blocks += int((~ok).sum())
            report.repaired_blocks += int(ok.sum())
            report.repaired_bytes += int((damaged[ok] != before[ok]).sum())
            grid[:, a + bad[ok]] = damaged[ok].T
        if task:
            task.advance(b - a)


def _ecc_open(img: Image.Image, task: Task | None) -> tuple[bytearray, EccReport] | None:
    """Repair an error-corrected image and return the data it carries, or None if it isn't one."""
    if cached := getattr(img, "_imagestore_ecc", None):  # inspect decodes an image more than once
        return cached
    buf = bytearray(img.width * img.height * BPP)  # filled a band at a time: tobytes() would be a second copy
    view, pos = memoryview(buf), 0
    for band in _Pixels(img)._iter_bands():
        view[pos:pos + len(band)] = band
        pos += len(band)
    view.release()
    blocks = len(buf) // ECC_BLOCK
    if blocks < ECC_MIN_BLOCKS or (parity := _ecc_detect(buf, blocks)) is None:
        return None
    rs, k = _rs(parity), ECC_BLOCK - parity
    report = EccReport(parity, blocks)
    if task:
        task.phase("Checking error correction", blocks)
    if np is not None:
        _ecc_repair_np(rs, buf, blocks, report, task)
    else:
        for i in range(blocks):
            block = bytearray(buf[i:ECC_BLOCK * blocks:blocks])
            if any(block):  # all-zero blocks are padding
                before = bytes(block)
                if not rs.repair(block):
                    report.failed_blocks += 1
                elif before != block:
                    report.repaired_blocks += 1
                    for m in range(ECC_BLOCK):
                        if before[m] != block[m]:
                            buf[m * blocks + i] = block[m]
                            report.repaired_bytes += 1
            if task and i % 256 == 255:
                task.advance(256)
    data = bytearray(blocks * k)
    for j in range(k):
        data[j::k] = buf[j * blocks:(j + 1) * blocks]
    img._imagestore_ecc = data, report
    return data, report



class _BufferPixels:
    """_Pixels over bytes already in memory (the data carried by an error-corrected image)."""

    def __init__(self, data: bytearray):
        self.data, self.total, self.pos = memoryview(data), len(data), 0

    def read(self, n: int) -> memoryview:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


def decode(img: Image.Image, task: Task | None = None) -> tuple[Meta, Iterator[bytes]]:
    """Parse the header and return (meta, original bytes in chunks).

    The size and SHA-256 checks run when the iterator is exhausted and raise ImageStoreError, so
    consume it fully before trusting the data (write to scratch space, then commit).
    task only reports progress while an error-corrected image is being checked.
    """
    px = _Pixels(img)
    head = px.read(HEADER.size)
    if len(head) < HEADER.size:
        raise ImageStoreError("image too small to contain an ImageStore header", "too_small")
    ecc = None
    if head[:4] != MAGIC or head[5] & FLAG_ECC:
        # error-corrected pixels hold interleaved codewords, so the header is not at the start
        if opened := _ecc_open(img, task):
            px, ecc = _BufferPixels(opened[0]), opened[1]
            head = px.read(HEADER.size)
        elif head[:4] == MAGIC:
            raise ImageStoreError("the header says this image has error correction, but none could be found", "ecc")
    magic, version, flags, orig, stored, digest, name_len = HEADER.unpack(head)
    if magic != MAGIC:
        if ecc and ecc.failed_blocks:
            raise ImageStoreError(_ecc_failure(ecc), "ecc")
        raise ImageStoreError("not an ImageStore image (bad magic)", "magic")
    if version != VERSION:
        raise ImageStoreError(f"unsupported ImageStore version {version}", "version")
    if HEADER.size + name_len + stored > px.total:
        raise ImageStoreError("image is truncated (payload extends past the pixel data)", "truncated")
    name = bytes(px.read(name_len)).decode("utf-8", errors="replace")
    meta = Meta(name, flags, orig, stored, digest, ecc)
    if bin(flags & FLAGS_PACKED).count("1") > 1:
        raise ImageStoreError("header marks the payload as stored more than one way", "version")
    chunks = _payload(px, stored)
    if flags & FLAG_ZLIB:
        chunks = _inflate(chunks, orig)
    elif flags & FLAG_LZMA:
        chunks = _unlzma(chunks, orig)
    elif flags & FLAG_ZSTD:
        chunks = _unzstd(chunks, orig)
    elif meta.squeezed:
        chunks = _unsqueeze(chunks, orig)
    chunks = _verified(meta, chunks)
    return meta, _blame_ecc(meta, chunks) if ecc and ecc.failed_blocks else chunks


def _ecc_failure(report: EccReport) -> str:
    return (f"damage beyond what error correction can repair: {report.failed_blocks:,} of {report.blocks:,} "
            f"blocks are too damaged ({report.parity} parity bytes per block repair up to "
            f"{report.parity // 2} damaged bytes each)")


def _blame_ecc(meta: Meta, chunks: Iterator[bytes]) -> Iterator[bytes]:
    """When blocks could not be repaired, say so instead of a bare checksum failure."""
    try:
        yield from chunks
    except ImageStoreError as e:
        if e.kind in ("sha", "size", "zlib", "squeeze"):
            raise ImageStoreError(f"{_ecc_failure(meta.ecc)} ({e})", "ecc") from e
        raise


def is_ignored(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in IGNORE_PATTERNS)


@dataclass
class IgnoreHit:
    """One top-level entry auto-ignore would skip (a matching folder is skipped whole)."""
    path: Path
    rel: str  # POSIX path relative to the scanned folder, as used by pack_dir(exclude=...)
    is_dir: bool
    files: int
    size: int


def scan_ignored_detail(path: Path) -> list[IgnoreHit]:
    """Find what auto-ignore would skip, entry by entry, so a user can review it."""
    hits: list[IgnoreHit] = []
    for dirpath, dirnames, filenames in os.walk(path):
        here = Path(dirpath)
        for n in [n for n in dirnames if is_ignored(n)]:
            dirnames.remove(n)  # don't descend: the whole subtree is skipped
            files = size = 0
            for dp, _, fns in os.walk(here / n):
                for fn in fns:
                    files += 1
                    try:
                        size += (Path(dp) / fn).lstat().st_size
                    except OSError:
                        pass
            hits.append(IgnoreHit(here / n, (here / n).relative_to(path).as_posix(), True, files, size))
        for n in [n for n in filenames if is_ignored(n)]:
            try:
                size = (here / n).lstat().st_size
            except OSError:
                size = 0
            hits.append(IgnoreHit(here / n, (here / n).relative_to(path).as_posix(), False, 1, size))
    return hits


def scan_ignored(path: Path) -> tuple[list[Path], int, int]:
    """Find what auto-ignore would skip. Returns (top-level hits, files inside them, bytes)."""
    hits = scan_ignored_detail(path)
    return [h.path for h in hits], sum(h.files for h in hits), sum(h.size for h in hits)


PACK_SMALL = 1 << 20  # files up to this size are read whole, once, by the prefetching threads
PACK_AHEAD = 64  # files read ahead of the tar writer; at most this many small files sit in memory


def _prefetch(name: str, size: int, hash_it: bool) -> tuple[bytes | None, bytes | None]:
    """(contents if the file is small, SHA-256 if asked for). Runs on a worker thread."""
    if size <= PACK_SMALL:
        with open(name, "rb") as f:
            data = f.read(size)
        return data, hashlib.sha256(data).digest() if hash_it else None
    if not hash_it:
        return None, None
    sha = hashlib.sha256()
    with open(name, "rb") as f:
        while chunk := f.read(CHUNK):
            sha.update(chunk)
    return None, sha.digest()


@dataclass
class PackStats:
    """Filled in by pack_dir: files stored as pointers to an identical earlier file, and their bytes."""
    duplicates: int = 0
    duplicate_bytes: int = 0


def _solid_key(ti: tarfile.TarInfo) -> tuple[str, str, str]:
    """Order for the files in a tar: by extension, then name, so files of a kind sit side by side
    and the compressor sees similar data together (7-Zip's solid ordering)."""
    base = ti.name.rpartition("/")[2]
    ext = base.rpartition(".")[2].lower() if "." in base.lstrip(".") else ""
    return ext, base, ti.name


def pack_dir(path: Path, ignore: bool = False, exclude: Collection[str] | None = None,
             on_entry: Callable[[tarfile.TarInfo], None] | None = None,
             stats: PackStats | None = None) -> tuple[BinaryIO, int]:
    """Tar a folder into scratch space (uncompressed: the payload encoders handle that). Returns (tar stream, file count).

    ignore skips every entry auto-ignore matches; exclude skips exactly the listed paths
    (POSIX, relative to the folder, as in IgnoreHit.rel), for a user-reviewed selection.
    on_entry is called for each entry kept (progress reporting; it may raise to abort).
    Folders come first, then everything else in _solid_key order. A file identical to an
    earlier one is stored as a tar hard link to it, without its data (counted in stats, when given);
    only files that share a size with another are hashed. Worker threads read small files and
    hash candidates ahead of the writer.
    """
    dirs, rest = _scan_dir(path, ignore, exclude, on_entry)
    tmp = _spool()
    return tmp, _write_tar(dirs, rest, tmp, stats)


def _scan_dir(path: Path, ignore: bool = False, exclude: Collection[str] | None = None,
              on_entry: Callable[[tarfile.TarInfo], None] | None = None) -> tuple[list, list]:
    """What pack_dir stores: (folder TarInfos, parents first; [(TarInfo, path)] of everything
    else in _solid_key order)."""
    root = path.name or "folder"
    skip = frozenset(exclude or ())
    dirs: list[tarfile.TarInfo] = []
    rest: list[tuple[tarfile.TarInfo, str]] = []
    with tarfile.open(fileobj=io.BytesIO(), mode="w:") as tar:  # for gettarinfo only

        def walk(name: str, arcname: str) -> None:
            if arcname != root and (ignore and is_ignored(PurePosixPath(arcname).name)
                                    or skip and arcname[len(root) + 1:] in skip):
                return  # a skipped folder takes its whole subtree with it
            ti = tar.gettarinfo(name, arcname)
            if ti is None:
                return  # sockets and the like, which tar cannot store (tar.add skips them too)
            if on_entry is not None:
                on_entry(ti)
            ti.uid = ti.gid = 0  # don't leak local user/group names into the image
            ti.uname = ti.gname = ""
            if ti.isdir():
                dirs.append(ti)
                for child in sorted(os.listdir(name)):
                    walk(os.path.join(name, child), f"{arcname}/{child}")
                return
            if ti.islnk():  # a hard link on disk: its target may sort after it, so store it as a
                ti.type, ti.linkname, ti.size = tarfile.REGTYPE, "", os.lstat(name).st_size  # file; dedup links it
            rest.append((ti, name))

        walk(str(path), root)
    rest.sort(key=lambda e: _solid_key(e[0]))
    return dirs, rest


def _tar_bounds(dirs: list, rest: list) -> tuple[int, int]:
    """(at least, at most) the size of the tar _write_tar makes from a _scan_dir result. The
    lower bound counts one plain header per entry and one file of each size (the first file of
    a size is never a duplicate); the upper one is only for progress."""
    block = tarfile.BLOCKSIZE
    regular = [ti.size for ti, _ in rest if ti.isreg()]
    padded = lambda n: -(-n // block) * block  # noqa: E731
    floor = block * (len(dirs) + len(rest) + 2) + sum(map(padded, set(regular)))
    ceiling = (sum(3 * block + 2 * len(ti.name) for ti in itertools.chain(dirs, (e[0] for e in rest)))
               + sum(map(padded, regular)) + tarfile.RECORDSIZE)
    return floor, ceiling


def _write_tar(dirs: list, rest: list, fileobj: BinaryIO, stats: PackStats | None = None) -> int:
    """Write a _scan_dir result as an uncompressed tar to fileobj. Returns the file count."""
    files = 0
    first: dict[tuple[int, bytes], str] = {}  # (size, SHA-256) -> the file stored with that content
    with tarfile.open(fileobj=fileobj, mode="w:", copybufsize=CHUNK) as tar, \
            ThreadPoolExecutor(WORKERS, thread_name_prefix="imagestore-pack") as pool:
        for ti in dirs:
            tar.addfile(ti)
        sizes = Counter(ti.size for ti, _ in rest if ti.isreg() and ti.size)
        ahead: deque = deque()
        jobs = iter(rest)
        for ti, name in rest:
            while len(ahead) < PACK_AHEAD and (job := next(jobs, None)):
                jti, jname = job
                ahead.append(pool.submit(_prefetch, jname, jti.size, sizes[jti.size] > 1) if jti.isreg() else None)
            pending = ahead.popleft()
            files += ti.isfile()
            if pending is None:
                tar.addfile(ti)
                continue
            data, digest = pending.result()
            if digest is not None:
                if original := first.get((ti.size, digest)):
                    if stats:
                        stats.duplicates += 1
                        stats.duplicate_bytes += ti.size
                    ti.type, ti.linkname, ti.size = tarfile.LNKTYPE, original, 0
                    tar.addfile(ti)
                    continue
                first[(ti.size, digest)] = ti.name
            if data is not None:
                tar.addfile(ti, io.BytesIO(data))
            else:
                with open(name, "rb") as f:
                    tar.addfile(ti, f)
    return files


PIPE_CHUNK = 8 << 20  # the pieces a _Pipe hands over: few hand-overs, little memory


class _Abandoned(Exception):
    """The reading side of a _Pipe has gone."""


class _Pipe:
    """Bytes from a writer thread to a reader, in PIPE_CHUNK-sized pieces with a bounded backlog.

    tarfile writes into it while the encoder reads chunks(), so a folder is packed and
    compressed at the same time and the tar never touches the disk. The writer ends with
    finish(), passing an exception to raise on the reading side if it failed; abandon() (the
    reading side gave up) makes the writer's next write raise _Abandoned."""
    _END = object()

    def __init__(self, depth: int = 8):
        self.queue: queue.Queue = queue.Queue(depth)
        self.buf = bytearray()
        self.written = 0
        self.abandoned = threading.Event()

    def tell(self) -> int:
        return self.written

    def write(self, data) -> int:
        self.buf += data
        self.written += len(data)
        if len(self.buf) >= PIPE_CHUNK:
            self._put(bytes(self.buf))
            self.buf.clear()
        return len(data)

    def _put(self, item) -> None:
        while not self.abandoned.is_set():
            try:
                self.queue.put(item, timeout=0.1)
                return
            except queue.Full:
                pass
        raise _Abandoned()

    def finish(self, error: BaseException | None = None) -> None:
        try:
            if error is None and self.buf:
                self._put(bytes(self.buf))
            self._put(self._END if error is None else error)
        except _Abandoned:
            pass

    def chunks(self) -> Iterator[bytes]:
        while (item := self.queue.get()) is not self._END:
            if isinstance(item, BaseException):
                raise item
            yield item

    def abandon(self) -> None:
        self.abandoned.set()


def _copy_pointer(member: tarfile.TarInfo, dest: Path) -> None:
    """Restore a hard-link entry as an independent copy, so editing one file never changes another."""
    m = tarfile.data_filter(member, str(dest))  # refuses targets outside dest
    src, dst = Path(dest, m.linkname), Path(dest, m.name)
    if not src.is_file():
        raise tarfile.TarError(f"{m.name} points at {m.linkname}, which is not a file in the archive")
    shutil.copyfile(src, dst)
    if m.mode is not None:
        os.chmod(dst, m.mode)
    os.utime(dst, (m.mtime, m.mtime))


def unpack_dir(chunks: Iterator[bytes], dest: Path) -> int:
    """Extract with the 'data' filter: blocks absolute paths, '..' escapes, devices, unsafe links.

    The tar is staged in scratch space first, so nothing lands in dest unless the image verified.
    """
    with _spool() as tmp:
        for chunk in chunks:
            tmp.write(chunk)
        tmp.seek(0)
        try:
            with tarfile.open(fileobj=tmp, mode="r:") as tar:
                members = tar.getmembers()
                files = sum(m.isfile() or m.islnk() for m in members)

                def extractable() -> Iterator[tarfile.TarInfo]:
                    # extractall pulls these lazily, so each pointer is copied right after its
                    # original was written and before folder timestamps are applied at the end
                    for m in members:
                        if m.islnk():
                            _copy_pointer(m, dest)
                        else:
                            yield m

                tar.extractall(dest, members=extractable(), filter="data")
        except (tarfile.TarError, OSError) as e:
            raise ImageStoreError(f"cannot extract folder: {e}", "extract") from e
    return files


def write_file(chunks: Iterator[bytes], out: Path) -> None:
    """Stream to a sibling temp file and rename on success, so a failed check never leaves bad output."""
    tmp = out.with_name(f".imagestore-{os.getpid()}.part")
    try:
        with open(tmp, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# Archives are as large as their payload: a 1 GiB file is a ~268 Mpx image, past Pillow's
# decompression-bomb guard (an error above ~179 Mpx). Opening is raised to 1 Gpx (4 GiB of
# pixels), still finite, while an archive is being read; the global is restored afterwards.
MAX_ARCHIVE_PIXELS = 1 << 30
_limit_lock = threading.Lock()


def open_image(path: Path) -> Image.Image:
    try:
        with _limit_lock:
            saved = Image.MAX_IMAGE_PIXELS
            if saved is not None and saved < MAX_ARCHIVE_PIXELS:
                Image.MAX_IMAGE_PIXELS = MAX_ARCHIVE_PIXELS
            try:
                img = Image.open(path)
            finally:
                Image.MAX_IMAGE_PIXELS = saved
        img.load()
    except (OSError, Image.DecompressionBombError) as e:
        raise ImageStoreError(f"cannot open {path}: {e}", "open") from e
    return img


def safe_name(name: str) -> str:
    """Stored names come from an untrusted image: keep the basename only."""
    base = Path(name.replace("\\", "/")).name
    return base if base not in ("", ".", "..") else "output.bin"


# -------------------------------------------------------------------- operations
# Whole operations (source -> verified output) shared by the terminal UI and the optional
# GUI. They report progress and honour cancellation through a Task, and they never report
# success before the output has been validated.


class Task:
    """Progress reporting and cooperative cancellation for one operation.

    progress(label, done, total) is called on phase changes and as bytes flow; total is
    None for steps that cannot be measured. cancel is anything with is_set() (such as a
    threading.Event); once set, the next check raises Cancelled and partial output is
    removed by the operation that was running.
    """

    def __init__(self, progress: Callable[[str, int, int | None], None] | None = None, cancel=None):
        self._progress = progress
        self._cancel = cancel
        self.label = ""
        self.done = 0
        self.total: int | None = None

    @property
    def cancelled(self) -> bool:
        return bool(self._cancel is not None and self._cancel.is_set())

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def phase(self, label: str, total: int | None = None) -> None:
        self.check()
        self.label, self.done, self.total = label, 0, total
        if self._progress:
            self._progress(label, 0, total)

    def advance(self, n: int) -> None:
        self.check()
        self.done += n
        if self._progress:
            self._progress(self.label, self.done, self.total)

    def track(self, chunks: Iterator[bytes], label: str, total: int | None) -> Iterator[bytes]:
        self.phase(label, total)
        for chunk in chunks:
            self.advance(len(chunk))
            yield chunk


class _TrackedReader:
    """Wraps the encoder's source stream to report progress and allow cancellation. The
    encoder reads the source once to hash (and compress) it and, when the payload is stored
    raw, a second time to copy it into the pixel buffer."""

    def __init__(self, raw: BinaryIO, task: Task, total: int, compress: bool):
        self.raw, self.task, self.total = raw, task, total
        self.passes = 0
        self.compress = compress

    def seek(self, pos: int, whence: int = 0) -> int:
        if pos == 0 and whence == 0:
            self.passes += 1
            if self.passes == 1:
                self.task.phase("Hashing and compressing" if self.compress else "Hashing", self.total)
            else:
                self.task.phase("Packing pixels", self.total)
        return self.raw.seek(pos, whence)

    def read(self, n: int = -1) -> bytes:
        data = self.raw.read(n)
        self.task.advance(len(data))
        return data

    def readinto(self, buf) -> int:
        n = self.raw.readinto(buf)
        self.task.advance(n or 0)
        return n


@dataclass
class EncodeResult:
    out: Path
    meta: Meta
    width: int
    height: int
    png_size: int
    verified: bool
    source: str
    compress_requested: bool
    files: int = 0
    skipped: int = 0  # entries left out of a folder by review; -1 = auto-ignore on (not counted)
    duplicates: int = 0  # folder files stored as pointers to an identical file
    duplicate_bytes: int = 0  # what those files would have added to the archive
    level: str = DEFAULT_LEVEL

    @property
    def header_size(self) -> int:
        return HEADER.size + len(self.meta.name.encode("utf-8"))

    @property
    def capacity(self) -> int:
        return self.width * self.height * BPP

    @property
    def padding(self) -> int:
        parity = self.meta.ecc.blocks * self.meta.ecc.parity if self.meta.ecc else 0
        return self.capacity - self.header_size - self.meta.stored_size - parity

    @property
    def saved_fraction(self) -> float:
        """How much smaller the stored payload is than the original (0 when stored raw)."""
        m = self.meta
        return 1 - m.stored_size / m.orig_size if m.orig_size and (m.compressed or m.squeezed) else 0.0


@dataclass
class DecodeResult:
    src: Path
    meta: Meta
    width: int
    height: int
    out: Path | None  # None when a text payload was decoded into memory
    text: str | None = None
    files: int = 0


@dataclass
class InspectResult:
    src: Path
    width: int
    height: int
    mode: str
    png_size: int
    meta: Meta | None  # None when the header itself could not be read
    ok: bool
    error: ImageStoreError | None = None
    computed_digest: bytes | None = None  # SHA-256 of the decoded payload, when fully decoded
    decoded_size: int = 0

    @property
    def capacity(self) -> int:
        return self.width * self.height * BPP


def default_png_path(src: Path) -> Path:
    return src.with_name(src.name + ".png")


def _check_output(out: Path, force: bool) -> None:
    if out.is_dir():
        raise ImageStoreError(f"{out} is a folder; choose a file name for the PNG", "exists")
    if out.exists() and not force:
        raise ImageStoreError(f"{out} already exists (use -f to overwrite)", "exists")
    if not out.absolute().parent.is_dir():
        raise ImageStoreError(f"the output folder {out.absolute().parent} does not exist", "missing")


def _verify_png(path: Path, meta: Meta, task: Task) -> None:
    """Decode a freshly written PNG and check its header and SHA-256 against what was encoded."""
    task.phase("Verifying written PNG")
    got, chunks = decode(open_image(path), task)
    if (got.name, got.flags, got.orig_size, got.stored_size, got.digest) != (
            meta.name, meta.flags, meta.orig_size, meta.stored_size, meta.digest):
        raise ImageStoreError("the written PNG's header does not match what was encoded", "verify")
    for _ in task.track(chunks, "Verifying written PNG", meta.orig_size):
        pass


def encode_stream(data: BinaryIO, name: str, out: Path, *, compress: bool = True, force: bool = False,
                  window: int = LZMA_WINDOW, ecc: float = 0, level: str = DEFAULT_LEVEL,
                  kind: int = 0, source: str = "", task: Task | None = None, verify: bool = True,
                  files: int = 0, skipped: int = 0, pack: PackStats | None = None) -> EncodeResult:
    """Encode a seekable stream to *out*: written to a temporary sibling, verified by decoding
    it again, and only then moved into place. Nothing is left behind on failure or cancel."""
    task = task or Task()
    out = Path(out)
    _check_output(out, force)
    total = data.seek(0, os.SEEK_END)
    data.seek(0)
    buf, (width, height), meta = encode_pixels(_TrackedReader(data, task, total, compress), name, compress, kind,
                                               window, ecc_parity_bytes(ecc), task, level)
    _save_png(out, buf, width, height, meta, task, verify, compress)
    return EncodeResult(out, meta, width, height, out.stat().st_size, verify, source or name, compress,
                        files, skipped, pack.duplicates if pack else 0, pack.duplicate_bytes if pack else 0, level)


def _save_png(out: Path, buf: bytearray, width: int, height: int, meta: Meta, task: Task, verify: bool,
              compress: bool) -> None:
    """Write to a temporary sibling, verify it by decoding it again, and only then move it into place.
    When compression was asked for, what is stored is already compressed or did not compress
    (likewise the parity), so the PNG stores it without deflating it again. buf is emptied once
    it is written: the caller's reference would otherwise keep it alive through verification."""
    task.phase("Writing PNG", len(buf) + height)
    tmp = out.with_name(f".{out.name}.imagestore-{os.getpid()}.part")
    try:
        write_png(tmp, buf, width, height, task, level=0 if compress else 1)
        buf.clear()  # free the pixel buffer before verification decodes a second copy
        task.check()
        if verify:
            _verify_png(tmp, meta, task)
        task.check()
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _encode_folder(src: Path, out: Path, *, compress: bool, window: int, ecc: float, level: str, ignore: bool,
                   exclude: Collection[str] | None, task: Task, verify: bool, skipped: int) -> EncodeResult:
    """Encode a folder with the tar streamed straight into the hash and the compressor: a writer
    thread packs while this one compresses (raises _RawWins in the rare case a plain tar is best)."""
    task.phase("Packing folder")  # done counts entries here, not bytes
    dirs, rest = _scan_dir(src, ignore, exclude, on_entry=lambda _ti: task.advance(1))
    floor, ceiling = _tar_bounds(dirs, rest)
    pack, pipe, counted = PackStats(), _Pipe(), []

    def produce() -> None:
        try:
            counted.append(_write_tar(dirs, rest, pipe, pack))
            pipe.finish()
        except BaseException as e:  # handed to the reading side
            pipe.finish(e)

    writer = threading.Thread(target=produce, name="imagestore-tar", daemon=True)
    writer.start()
    try:
        chunks = task.track(pipe.chunks(), "Hashing and compressing" if compress else "Hashing", ceiling)
        buf, (width, height), meta = _pack_payload(chunks, floor, None, portable_name(src.name) or "folder",
                                                   compress, FLAG_DIR, window, ecc_parity_bytes(ecc), task, level)
    finally:
        pipe.abandon()
        writer.join()
    _save_png(out, buf, width, height, meta, task, verify, compress)
    return EncodeResult(out, meta, width, height, out.stat().st_size, verify, f"folder {src}", compress,
                        counted[0], skipped, pack.duplicates, pack.duplicate_bytes, level)


def portable_name(name: str) -> str:
    """File names that are not valid UTF-8 (possible on Linux) cannot be stored as UTF-8;
    keep them readable with U+FFFD for the undecodable bytes. The content is unaffected."""
    try:
        name.encode("utf-8")
        return name
    except UnicodeEncodeError:
        return os.fsencode(name).decode("utf-8", errors="replace")


def encode_path(src: Path, out: Path | None = None, *, compress: bool = True, force: bool = False,
                window: int = LZMA_WINDOW, ecc: float = 0, level: str = DEFAULT_LEVEL,
                ignore: bool = False, exclude: Collection[str] | None = None, task: Task | None = None,
                verify: bool = True) -> EncodeResult:
    task = task or Task()
    src = Path(src)
    if src.is_dir():
        src = src.resolve()
        out = Path(out) if out else default_png_path(src)
        _check_output(out, force)  # fail before packing a large folder
        # -1: auto-ignore was on, but counting its hits would mean walking them; not worth it
        skipped = len(exclude) if exclude else (-1 if ignore else 0)
        try:
            return _encode_folder(src, out, compress=compress, window=window, ecc=ecc, level=level, ignore=ignore,
                                  exclude=exclude, task=task, verify=verify, skipped=skipped)
        except _RawWins:
            pass  # rare (nothing to squeeze or compress): store the tar itself, kept in scratch space this time
        task.phase("Packing folder")
        pack = PackStats()
        tar, files = pack_dir(src, ignore, exclude=exclude, on_entry=lambda _ti: task.advance(1), stats=pack)
        with tar:
            return encode_stream(tar, portable_name(src.name) or "folder", out, compress=compress, force=force, window=window, ecc=ecc, level=level, kind=FLAG_DIR,
                                 source=f"folder {src}", task=task, verify=verify, files=files, skipped=skipped, pack=pack)
    if src.is_file():
        out = Path(out) if out else default_png_path(src)
        _check_output(out, force)
        with src.open("rb") as f:
            return encode_stream(f, portable_name(src.name), out, compress=compress, force=force, window=window, ecc=ecc, level=level, source=str(src), task=task,
                                 verify=verify)
    raise ImageStoreError(f"no such file or folder: {src}", "missing")


def encode_text(text: str, out: Path | None = None, *, compress: bool = True, force: bool = False,
                window: int = LZMA_WINDOW, ecc: float = 0, level: str = DEFAULT_LEVEL,
                task: Task | None = None, verify: bool = True) -> EncodeResult:
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ImageStoreError(f"the text cannot be encoded as UTF-8: {e}", "text") from e
    return encode_stream(io.BytesIO(data), TEXT_NAME, Path(out or "text.png"), compress=compress, window=window, ecc=ecc, level=level,
                         force=force, kind=FLAG_TEXT, source=f"text ({len(text):,} chars)", task=task, verify=verify)


def read_header(src: Path) -> tuple[Image.Image, Meta]:
    """Open an image and parse its ImageStore header (no payload is decoded yet)."""
    img = open_image(Path(src))
    meta, _ = decode(img)
    return img, meta


def decode_path(src: Path, out_dir: Path | None = None, *, force: bool = False, text_in_memory: bool = True,
                task: Task | None = None) -> DecodeResult:
    """Restore the payload. Output appears only after the size and SHA-256 checks pass:
    files are staged in a temporary sibling, folders in scratch space."""
    task = task or Task()
    src = Path(src)
    task.phase("Reading image")
    img = open_image(src)
    meta, chunks = decode(img, task)
    width, height = img.size
    del img  # chunks keeps it while it is still read; an error-corrected image is already copied out
    chunks = task.track(chunks, "Decoding and verifying", meta.orig_size)
    if meta.is_text and text_in_memory:
        data = b"".join(chunks)
        return DecodeResult(src, meta, width, height, None, text=data.decode("utf-8", errors="replace"))
    base = Path(out_dir) if out_dir else src.parent
    out = base / safe_name(meta.name)
    if out.exists() and not force:
        raise ImageStoreError(f"{out} already exists (use -f to overwrite; folders are merged into)", "exists")
    if out.exists() and out.is_dir() != meta.is_dir:
        raise ImageStoreError(f"{out} exists as a different type (file vs folder)", "exists")
    created = not base.exists()
    base.mkdir(parents=True, exist_ok=True)
    try:
        if meta.is_dir:
            files = _restore_dir(chunks, base)
        else:
            files = 0
            write_file(chunks, out)
    except BaseException:
        if created:
            try:
                base.rmdir()  # only if still empty
            except OSError:
                pass
        raise
    return DecodeResult(src, meta, width, height, out, files=files)


def _restore_dir(chunks: Iterator[bytes], base: Path) -> int:
    """Extract into a staging folder inside base, then move the result into place, so an
    extraction that fails part-way (a refused member, a full disk) leaves nothing behind."""
    staging = Path(tempfile.mkdtemp(prefix=".imagestore-", suffix=".staging", dir=base))
    try:
        files = unpack_dir(chunks, staging)
        for entry in staging.iterdir():
            _merge(entry, base / entry.name)
        return files
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _merge(src: Path, dst: Path) -> None:
    """Move src to dst; folders that exist on both sides are merged (as tar extraction did)."""
    if not dst.exists() and not dst.is_symlink():
        os.replace(src, dst)
    elif src.is_dir() and not src.is_symlink() and dst.is_dir() and not dst.is_symlink():
        for child in src.iterdir():
            _merge(child, dst / child.name)
    elif dst.is_dir() and not dst.is_symlink():
        raise ImageStoreError(f"cannot replace the folder {dst} with a file", "extract")
    else:
        os.replace(src, dst)


def inspect_path(src: Path, task: Task | None = None) -> InspectResult:
    """Read the header and verify the whole payload, collecting everything worth showing."""
    task = task or Task()
    src = Path(src)
    task.phase("Reading image")
    img = open_image(src)
    width, height = img.size
    base = dict(src=src, width=width, height=height, mode=img.mode, png_size=src.stat().st_size)
    try:
        meta, chunks = decode(img, task)
    except Cancelled:
        raise
    except ImageStoreError as e:
        return InspectResult(**base, meta=None, ok=False, error=e)
    n, error = 0, None
    try:
        for chunk in task.track(chunks, "Verifying payload", meta.orig_size):
            n += len(chunk)
    except Cancelled:
        raise
    except ImageStoreError as e:
        error = e
    computed = meta.digest if error is None else None
    if error is not None and error.kind in ("sha", "size"):
        # Only on failure: decode again to show the digest of what the image actually holds
        # (the check itself already hashed it once; a passing check needs no second pass).
        sha = hashlib.sha256()
        try:
            for chunk in task.track(decode(img, task)[1], "Computing SHA-256 of the altered payload", meta.orig_size):
                sha.update(chunk)
        except Cancelled:
            raise
        except ImageStoreError:
            pass  # the same failure again, after the last chunk: the digest is complete
        computed = sha.digest()
    return InspectResult(**base, meta=meta, ok=error is None, error=error, computed_digest=computed, decoded_size=n)


# ------------------------------------------------------------------------ errors


@dataclass
class Explanation:
    title: str
    hint: str
    detail: str


_EXPLAIN = {
    "magic": ("Not an ImageStore image",
              "This PNG does not start with an ImageStore header. It was made by something else, or its "
              "pixels were changed so much that the header is gone (for an image with error correction, "
              "so much that it can no longer be recognised)."),
    "mode": ("Unsupported PNG colour mode",
             "ImageStore archives are RGBA PNGs, and every channel (alpha too) carries data. Some app "
             "converted this one, which usually means the alpha channel was stripped. Use the original file."),
    "too_small": ("Image too small",
                  "The image has fewer pixels than an ImageStore header needs. It is not an ImageStore "
                  "archive, or it was cropped or resized."),
    "version": ("Unsupported archive version",
                "This archive uses a format version this ImageStore cannot read. Version 1 (CRC32) "
                "archives are no longer supported."),
    "truncated": ("Image is truncated",
                  "The header promises more data than the image holds. The PNG was probably cropped, "
                  "resized or only partly copied."),
    "zlib": ("Compressed payload is corrupted",
             "The zlib, LZMA or zstd stream inside the image could not be decompressed. Pixel values changed after "
             "encoding (lossy re-compression, colour management or alpha premultiplication). Nothing is "
             "written when this check fails."),
    "no_lzma": ("This Python cannot read LZMA",
                "The image is LZMA-compressed, but the Python running ImageStore was built without the lzma "
                "module (usually because the xz development files were missing when it was compiled). "
                "Use a Python that has it, or decode on another machine."),
    "no_zstd": ("This Python cannot read zstd",
                "The image is zstd-compressed (the default since ImageStore added compression levels), and "
                "zstd is built into Python 3.14 and newer. On an older Python, install it with "
                "pip install backports.zstd."),
    "ecc": ("Too damaged for error correction",
            "This image has error correction, but more of it is damaged than the parity can repair, "
            "or its error-correction data is missing. Nothing is written when this check fails; try "
            "an earlier or untouched copy of the PNG."),
    "memory": ("Not enough memory to compress",
               "The compression window asked for needs more RAM than is available. Choose a smaller window "
               "(--window in the CLI) or turn compression off. Nothing was written."),
    "squeeze": ("Packed folder data is corrupted",
                "The zero-run encoding inside the image is malformed. Pixel values changed after "
                "encoding (lossy re-compression, colour management or alpha premultiplication). Nothing is "
                "written when this check fails."),
    "size": ("Integrity check failed (size mismatch)",
             "The payload decoded to a different length than the header records, so the pixels were "
             "altered. Nothing is written when this check fails."),
    "sha": ("Integrity check failed (SHA-256 mismatch)",
            "The payload decoded, but its checksum does not match the one stored at encode time. Some "
            "pixel values changed after encoding. Lossy re-encoding, alpha premultiplication and "
            "colour-profile conversion are the usual causes; chat apps, image hosts and many editors do "
            "this. Nothing is written when this check fails; use an untouched copy of the PNG."),
    "verify": ("Written PNG failed verification",
               "The PNG was written but did not decode back to the encoded data, so it was discarded. "
               "The disk or file system may be faulty."),
    "extract": ("Folder could not be extracted",
                "The archive verified, but extracting it failed. Check the destination's free space "
                "and permissions."),
    "text": ("Text cannot be stored",
             "The text contains characters that are not valid Unicode (often from a damaged paste or "
             "terminal). Remove them and try again."),
    "exists": ("Output already exists",
               "Choose another location, or allow overwriting (-f on the command line)."),
    "missing": ("Not found", "Check the path; it may have been moved, renamed or deleted."),
    "source_changed": ("Source changed during encoding",
                       "The file changed size while it was being read. Wait until whatever is writing "
                       "it finishes, then try again."),
    "cancelled": ("Cancelled", "The operation stopped and nothing was written."),
}


def _oserror_explanation(e: OSError) -> tuple[str, str] | None:
    import errno
    where = f" ({e.filename})" if getattr(e, "filename", None) else ""
    if e.errno in (errno.EACCES, errno.EPERM):
        return "Permission denied", f"ImageStore was not allowed to read or write a file{where}. Choose " \
                                    "another location or check the file's permissions."
    if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return "Not enough disk space", f"The disk filled up while writing{where}. Free some space or " \
                                        "choose another location; any partial output was removed."
    if e.errno == errno.ENOENT:
        return "Not found", f"A file or folder does not exist{where}. Check the path."
    if e.errno == errno.EROFS:
        return "Read-only location", f"The destination is on a read-only file system{where}."
    if e.errno == errno.EISDIR:
        return "That is a folder", f"A file was expected but a folder was given{where}."
    if e.errno == errno.ENAMETOOLONG:
        return "Name too long", "The output path is longer than the file system allows."
    return None


def explain(exc: BaseException) -> Explanation:
    """Turn an exception into a short title, an actionable hint and the technical detail."""
    detail = str(exc) or type(exc).__name__
    if isinstance(exc, ImageStoreError):
        cause = exc.__cause__
        if exc.kind == "open" and isinstance(cause, OSError) and (found := _oserror_explanation(cause)):
            return Explanation(*found, detail)
        if exc.kind == "open":
            if isinstance(cause, Image.DecompressionBombError):
                return Explanation("Image too large to open safely",
                                   "Pillow refused the image as a possible decompression bomb.", detail)
            return Explanation("Cannot open image",
                               "The file is not a readable image, or it is damaged. ImageStore archives "
                               "are PNG files.", detail)
        if exc.kind in _EXPLAIN:
            return Explanation(*_EXPLAIN[exc.kind], detail)
        return Explanation("ImageStore error", "", detail)
    if isinstance(exc, MemoryError):
        return Explanation("Not enough memory",
                           "The image or payload is too large to process in the available memory. Close "
                           "other programs, or split the data into smaller archives.", detail)
    if isinstance(exc, OSError) and (found := _oserror_explanation(exc)):
        return Explanation(*found, detail)
    return Explanation(type(exc).__name__, "", detail)


# ---------------------------------------------------------------------------- ui

TAGLINE = "Turn files into pixels. Recover them exactly."
err_console = Console(stderr=True)


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return str(n)


def show_path(path: Path) -> str:
    """A path as short as it can be while still unambiguous: relative to the working
    directory when inside it, else with ~ for the home folder."""
    path = Path(path)
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve())) or "."
    except (ValueError, OSError):
        pass
    try:
        return "~/" + str(path.resolve().relative_to(Path.home().resolve()))
    except (ValueError, OSError, RuntimeError):
        return str(path)


def report(title: str, rows: list[tuple[str, str]], style: str = "green") -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(overflow="fold")
    for k, v in rows:
        table.add_row(k, v)
    console.print(Panel(table, title=title, title_align="left", border_style=style, expand=False, padding=(0, 1)))


def render_error(exc: BaseException) -> None:
    """Errors go to stderr so piped output (such as decoded text) is never polluted."""
    ex = explain(exc)
    err_console.print(f"[bold red]error:[/] [bold]{escape(ex.title)}[/] [dim]—[/] {escape(ex.detail)}")
    if ex.hint:
        err_console.print(f"  [dim]{escape(ex.hint)}[/]", soft_wrap=False)


class _CliProgress:
    """A transient rich progress bar driven by a Task (only on an interactive terminal)."""

    def __init__(self):
        self.progress = None
        self.task_id = None

    def __enter__(self) -> Task:
        if console.is_terminal:
            from rich.progress import (BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn,
                                       TimeRemainingColumn)
            self.progress = Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(bar_width=32),
                                     DownloadColumn(binary_units=True), TimeRemainingColumn(),
                                     console=console, transient=True)
            self.progress.__enter__()
            self.task_id = self.progress.add_task("Starting…", total=None)
        return Task(self._update)

    def _update(self, label: str, done: int, total: int | None) -> None:
        if self.progress is not None:  # unmeasured steps get a pulsing bar, not a byte count
            self.progress.update(self.task_id, description=label, completed=done if total else 0, total=total)

    def __exit__(self, *exc) -> None:
        if self.progress is not None:
            self.progress.__exit__(*exc)


def _payload_line(r: EncodeResult) -> str:
    m = r.meta
    if m.compressed:
        return f"{human(m.stored_size)}  [dim]{m.method} ·[/] [green]{r.saved_fraction:.1%} smaller[/]"
    if m.squeezed:
        return f"{human(m.stored_size)}  [dim]zero padding removed ·[/] [green]{r.saved_fraction:.1%} smaller[/]"
    if r.compress_requested:
        return f"{human(m.stored_size)}  [dim]raw · compression would not have made it smaller[/]"
    return f"{human(m.stored_size)}  [dim]raw · compression off[/]"


def ecc_summary(e: EccReport, decoding: bool = True) -> tuple[str, str]:
    """Plain-text line about error correction and its tone ("success", "warning", "danger" or "")."""
    strength = f"{e.overhead:.1%} parity, repairs up to ~{e.tolerance:.1%} damage"
    if not decoding:
        return strength, ""
    if e.failed_blocks:
        return f"{e.failed_blocks:,} of {e.blocks:,} blocks beyond repair · {strength}", "danger"
    if e.repaired_bytes:
        return (f"Repaired {e.repaired_bytes:,} damaged bytes in {e.repaired_blocks:,} of {e.blocks:,} blocks · "
                f"{strength}"), "warning"
    return f"Undamaged · {strength}", "success"


def ecc_text(e: EccReport, decoding: bool = True) -> str:
    """One line about error correction, as rich markup."""
    strength = f"{e.overhead:.1%} parity [dim]· repairs up to ~{e.tolerance:.1%} damage[/]"
    if not decoding:
        return strength
    if e.failed_blocks:
        return f"[red]✗ {e.failed_blocks:,} of {e.blocks:,} blocks beyond repair[/]  [dim]{strength}[/]"
    if e.repaired_bytes:
        return (f"[yellow]repaired {e.repaired_bytes:,} damaged bytes[/] [dim]in {e.repaired_blocks:,} of "
                f"{e.blocks:,} blocks[/]  [dim]{strength}[/]")
    return f"[green]✓ undamaged[/]  [dim]{strength}[/]"


def render_encode(r: EncodeResult) -> None:
    m = r.meta
    if r.meta.is_dir:
        skipped = {0: "", -1: ", auto-ignore on"}.get(r.skipped, f", {r.skipped:,} entries left out")
        what = f"folder [bold]{escape(m.name)}/[/]  [dim]{r.files:,} files{skipped}[/]"
    elif m.is_text:
        what = escape(r.source)
    else:
        what = f"file [bold]{escape(m.name)}[/]"
    rows = [
        ("Source", what),
        ("Output", escape(show_path(r.out))),
        ("Original", human(m.orig_size)),
        ("Stored", _payload_line(r)),
        *([("Note", f"[yellow]{note}[/]")] if r.compress_requested and (note := fallback_note(r.level)) else []),
        *([("Error correction", ecc_text(m.ecc, decoding=False))] if m.ecc else []),
        *([("Tip", "[dim]pip install numpy makes error correction several times faster[/]")]
          if m.ecc and np is None and r.capacity > ECC_HINT_BYTES else []),
        *([("Duplicates", f"{r.duplicates:,} identical {'file' if r.duplicates == 1 else 'files'} stored once  "
                          f"[dim]· {human(r.duplicate_bytes)} not stored[/]")] if r.duplicates else []),
        ("PNG file", f"{human(r.png_size)} on disk"),
        ("Image", f"{r.width} × {r.height} px RGBA  [dim]· {r.width * r.height:,} pixels · "
                  f"{human(r.padding)} padding[/]"),
        ("SHA-256", f"[dim]{m.digest.hex()}[/]"),
        ("Integrity", "[green]✓ verified[/] [dim]decoded the written PNG; SHA-256 matches[/]" if r.verified
         else "[yellow]not verified[/] [dim](--no-verify)[/]"),
    ]
    report("[green]✓[/] Encoded" + (" and verified" if r.verified else ""), rows, "green")


def render_decode(r: DecodeResult) -> None:
    m = r.meta
    if r.out is None:
        if sys.stdout.isatty():
            console.print(Panel(Text(r.text or ""), title="Decoded text", title_align="left", border_style="green",
                                expand=False, subtitle=f"{m.orig_size:,} bytes · SHA-256 verified",
                                subtitle_align="right"))
        else:
            sys.stdout.write(r.text or "")
        return
    detail = f"{r.files:,} files, {human(m.orig_size)} archived" if m.is_dir else human(m.orig_size)
    report("[green]✓[/] Decoded", [
        ("Image", escape(show_path(r.src))),
        ("Restored", f"{escape(show_path(r.out))}{'/' if m.is_dir else ''}  [dim]({detail})[/]"),
        *([("Error correction", ecc_text(m.ecc))] if m.ecc else []),
        ("Integrity", "[green]✓ SHA-256 verified[/] [dim]before anything was written[/]"),
    ])


def _kind_text(m: Meta) -> str:
    if m.is_text:
        return "text string"
    return f"{'folder' if m.is_dir else 'file'}: {escape(m.name)}"


def render_inspect(r: InspectResult) -> None:
    rows = [("Image", f"{escape(show_path(r.src))}  [dim]({r.width} × {r.height} {r.mode}, "
                      f"{human(r.png_size)} on disk)[/]")]
    m = r.meta
    if m is None:
        ex = explain(r.error)
        rows += [("Archive", f"[red]✗ {escape(ex.title)}[/]"), ("Detail", f"[dim]{escape(ex.detail)}[/]")]
        if ex.hint:
            rows.append(("Hint", escape(ex.hint)))
        report("[red]✗[/] Not readable as ImageStore", rows, "red")
        return
    used = HEADER.size + len(m.name.encode("utf-8")) + m.stored_size
    rows += [
        ("Type", _kind_text(m)),
        ("Original", human(m.orig_size)),
        ("Stored", f"{human(m.stored_size)}  [dim]" + (
            f"{m.method} · {1 - m.stored_size / m.orig_size:.1%} smaller"
            if (m.compressed or m.squeezed) and m.orig_size else "raw")
         + "[/]"),
        ("Capacity", f"{human(r.capacity)} across {r.width * r.height:,} pixels  "
                     f"[dim]({used / r.capacity:.1%} used)[/]" if r.capacity else "0 B"),
        *([("Error correction", ecc_text(m.ecc))] if m.ecc else []),
        ("Format", f"[dim]v{VERSION} · flags 0x{m.flags:02x}"
                   f"{' (' + ', '.join(n for f, n in ((FLAG_ZLIB, 'zlib'), (FLAG_LZMA, 'lzma'), (FLAG_ZSTD, 'zstd'), (FLAG_TEXT, 'text'), (FLAG_DIR, 'folder'), (FLAG_SQUEEZE, 'squeezed'), (FLAG_ECC, 'ecc')) if m.flags & f) + ')' if m.flags else ''}[/]"),
        ("SHA-256", f"[dim]{m.digest.hex()}[/]  [dim](stored)[/]"),
    ]
    if r.ok:
        rows.append(("Integrity", "[green]✓ OK[/] [dim]payload decodes and SHA-256 matches[/]"))
        report("ImageStore info", rows, "cyan")
        return
    ex = explain(r.error)
    if r.computed_digest is not None:
        rows.append(("Computed", f"[red]{r.computed_digest.hex()}[/]"))
    rows += [("Integrity", f"[red]✗ FAILED[/] — {escape(ex.title)}"), ("Detail", f"[dim]{escape(ex.detail)}[/]")]
    if ex.hint:
        rows.append(("Hint", escape(ex.hint)))
    report("ImageStore info", rows, "red")


def _review_ignored(hits: list[IgnoreHit]) -> set[str]:
    """Let the user pick which auto-ignore hits to keep. Returns the paths to exclude."""
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Entry")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    for i, h in enumerate(hits, 1):
        table.add_row(str(i), escape(h.rel + ("/" if h.is_dir else "")), f"{h.files:,}", human(h.size))
    console.print(table)
    answer = Prompt.ask("Numbers to [bold]keep[/] [dim](e.g. 1,3-5; Enter skips them all)[/]", default="")
    keep: set[int] = set()
    for part in answer.replace(" ", "").split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        if lo.isdigit() and (not hi or hi.isdigit()):
            keep.update(range(int(lo), int(hi or lo) + 1))
        else:
            console.print(f"[yellow]ignored {escape(part)!r}: not a number or range[/]")
    excluded = {h.rel for i, h in enumerate(hits, 1) if i not in keep}
    console.print(f"[dim]Keeping {len(hits) - len(excluded)}, skipping {len(excluded)}.[/]")
    return excluded


def ask_ignore(src: Path) -> tuple[bool, set[str] | None]:
    """Scan for junk and ask what to do. Returns (ignore, exclude) for encode_path.
    Nothing found -> no question."""
    with console.status("Scanning for ignorable files…"):
        hits = scan_ignored_detail(src)
    if not hits:
        return False, None
    files, size = sum(h.files for h in hits), sum(h.size for h in hits)
    names = ", ".join(escape(h.rel + ("/" if h.is_dir else "")) for h in hits[:5])
    more = f" … +{len(hits) - 5} more" if len(hits) > 5 else ""
    console.print(f"[yellow]Auto-ignore[/] would skip {len(hits)} entries "
                  f"({files:,} files, {human(size)}): {names}{more}")
    choice = Prompt.ask("Skip them? [dim](y = skip, n = keep everything, r = review one by one)[/]",
                        choices=["y", "n", "r"], default="y", show_choices=False)
    if choice == "y":
        return True, None
    if choice == "n":
        return False, None
    return False, _review_ignored(hits)


def do_encode(src: Path, out: Path | None, compress: bool, force: bool,
              ignore: bool | None = None, verify: bool = True, window: int = LZMA_WINDOW, ecc: float = 0,
              level: str = DEFAULT_LEVEL) -> EncodeResult:
    """ignore: True/False decides auto-ignore for folders; None asks."""
    exclude = None
    if ignore is None:
        ignore, exclude = ask_ignore(Path(src).resolve()) if Path(src).is_dir() else (False, None)
    with _CliProgress() as task:
        result = encode_path(src, out, compress=compress, force=force, ignore=ignore, exclude=exclude, task=task,
                             verify=verify, window=window, ecc=ecc, level=level)
    render_encode(result)
    return result


def do_encode_text(text: str, out: Path | None, compress: bool, force: bool, verify: bool = True,
                   window: int = LZMA_WINDOW, ecc: float = 0, level: str = DEFAULT_LEVEL) -> EncodeResult:
    with _CliProgress() as task:
        result = encode_text(text, out, compress=compress, force=force, window=window, ecc=ecc, level=level, task=task,
                             verify=verify)
    render_encode(result)
    return result


def do_decode(src: Path, out_dir: Path | None, force: bool) -> DecodeResult:
    """Text payloads are printed (raw if piped) unless an output dir is given."""
    with _CliProgress() as task:
        result = decode_path(src, out_dir, force=force, text_in_memory=out_dir is None, task=task)
    render_decode(result)
    return result


def do_info(src: Path) -> InspectResult:
    with _CliProgress() as task:
        result = inspect_path(src, task)
    render_inspect(result)
    return result


def clean_path(text: str) -> Path:
    """Tolerate drag-and-drop paths from the terminal (quotes, backslash-escaped spaces)."""
    text = text.strip().strip("'\"").replace("\\ ", " ")
    return Path(text).expanduser()


BROWSE_ROWS = 40


def browse(start: Path, images_only: bool = False, allow_dir: bool = False) -> Path:
    """Tiny file browser: pick by number, '..' to go up, or type/drop any path.

    With allow_dir, '.' picks the folder currently shown.
    """
    cwd = start.resolve()
    while True:
        try:
            entries = [p for p in cwd.iterdir() if not p.name.startswith(".")]
        except OSError as e:
            render_error(e)
            cwd = cwd.parent
            continue
        if images_only:
            entries = [p for p in entries if p.is_dir() or p.suffix.lower() == ".png"]
        entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
        shown = entries[:BROWSE_ROWS]

        table = Table(title=f"[bold]{escape(show_path(cwd))}[/]", title_justify="left", header_style="dim",
                      box=None, pad_edge=False)
        table.add_column("#", justify="right", style="cyan")
        table.add_column("Name")
        table.add_column("Size", justify="right", style="dim")
        for i, p in enumerate(shown, 1):
            if p.is_dir():
                table.add_row(str(i), f"[bold blue]{escape(p.name)}/[/]", "")
            else:
                try:
                    size = human(p.stat().st_size)
                except OSError:
                    size = "?"
                table.add_row(str(i), escape(p.name), size)
        console.print(table)
        if not entries:
            console.print("[dim]  (nothing here" + (" but folders and PNGs are shown" if images_only else "") + ")[/]")
        if len(entries) > len(shown):
            console.print(f"[dim]… {len(entries) - len(shown)} more; type a path to reach them[/]")

        hint = "number, .. = up, . = this folder, or a path" if allow_dir else "number, .. = up, or a path"
        answer = Prompt.ask(f"[bold]Pick[/] [dim]({hint}; drag-and-drop works)[/]").strip()
        if not answer:
            continue
        if answer == "." and allow_dir:
            return cwd
        if answer == "..":
            cwd = cwd.parent
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            chosen = shown[int(answer) - 1]
        else:
            chosen = clean_path(answer)
            if not chosen.is_absolute():
                chosen = cwd / chosen
        if chosen.is_dir():
            cwd = chosen.resolve()
            continue
        if not chosen.exists():
            console.print(f"[yellow]{escape(str(chosen))} does not exist[/]")
            continue
        return chosen


MENU = (
    ("e", "Encode", "a file or folder into a PNG"),
    ("t", "Encode text", "type or paste a message"),
    ("d", "Decode", "restore the original from a PNG"),
    ("i", "Inspect", "read metadata and verify integrity"),
    ("g", "GUI", "open the graphical interface"),
    ("h", "Help", "command-line usage"),
    ("q", "Quit", ""),
)


def _banner() -> None:
    title = Text.assemble(("ImageStore", "bold cyan"), "  ", (TAGLINE, "dim"))
    console.print(Panel(title, border_style="cyan", expand=False, padding=(0, 2),
                        subtitle=f"[dim]format v{VERSION} · RGBA · SHA-256[/]", subtitle_align="right"))


def _menu(here: Path) -> None:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column(style="bold")
    grid.add_column(style="dim")
    for key, name, desc in MENU:
        grid.add_row(f" {key}", name, desc)
    console.print()
    console.print(grid)
    try:
        where = "~/" + str(here.resolve().relative_to(Path.home().resolve()))
    except (ValueError, OSError, RuntimeError):
        where = str(here)
    console.print(f"[dim]  in {escape(where)}[/]")


def _ask_output(default: Path) -> tuple[Path, bool] | None:
    out = clean_path(Prompt.ask("Output PNG", default=show_path(default)))
    if out.exists() and not Confirm.ask(f"{escape(show_path(out))} exists. Overwrite?", default=False):
        return None
    return out, out.exists()


def _ask_level() -> str:
    return Prompt.ask("Compression [dim](fast and balanced: zstd; max: LZMA, smallest but slower; "
                      "kept only if it makes the payload smaller)[/]", choices=[*LEVELS, "off"], default=DEFAULT_LEVEL)


def _level_or_default(level: str) -> str:
    return level if level in LEVELS else DEFAULT_LEVEL


def _interactive_encode(here: Path) -> Path:
    src = browse(here, allow_dir=True)
    exclude, ignore = None, False
    if src.is_dir():
        src = src.resolve()
        ignore, exclude = ask_ignore(src)
    else:
        console.print(f"[dim]Selected[/] {escape(src.name)} [dim]({human(src.stat().st_size)})[/]")
    picked = _ask_output(default_png_path(src))
    if picked is None:
        return src.parent
    out, force = picked
    level = _ask_level()
    with _CliProgress() as task:
        result = encode_path(src, out, compress=level != "off", level=_level_or_default(level), force=force,
                             ignore=ignore, exclude=exclude, task=task)
    render_encode(result)
    return src.parent


def _interactive_text() -> None:
    console.print("[dim]Enter text, finish with a blank line:[/]")
    lines = []
    while (line := input()) != "":
        lines.append(line)
    text = "\n".join(lines)
    if not text:
        raise ImageStoreError("no text entered", "missing")
    picked = _ask_output(Path("text.png"))
    if picked is None:
        return
    out, force = picked
    level = _ask_level()
    do_encode_text(text, out, level != "off", force, level=_level_or_default(level))


def _interactive_decode(here: Path) -> Path:
    src = browse(here, images_only=True)
    with console.status("Reading header…"):
        _, meta = read_header(src)
    console.print(f"[dim]Contains[/] {_kind_text(meta)} [dim]· {human(meta.orig_size)}"
                  f"{' · ' + meta.method if meta.method != 'raw' else ''}[/]")
    if meta.is_text:
        do_decode(src, None, False)
        return src.parent
    dest = clean_path(Prompt.ask("Restore into folder", default=show_path(src.parent)))
    target = dest / safe_name(meta.name)
    force = False
    if target.exists():
        what = "Merge into" if meta.is_dir and target.is_dir() else "Overwrite"
        if not Confirm.ask(f"{escape(show_path(target))} exists. {what} it?", default=False):
            return src.parent
        force = True
    do_decode(src, dest, force)
    return src.parent


def interactive() -> None:
    _banner()
    here = Path.cwd()
    while True:
        _menu(here)
        choice = Prompt.ask("Action", choices=[k for k, _, _ in MENU], default="e", show_choices=False)
        if choice == "q":
            return
        try:
            if choice == "e":
                here = _interactive_encode(here)
            elif choice == "t":
                _interactive_text()
            elif choice == "d":
                here = _interactive_decode(here)
            elif choice == "i":
                src = browse(here, images_only=True)
                here = src.parent
                do_info(src)
            elif choice == "g":
                console.print("[dim]Opening the ImageStore window; close it to come back here.[/]")
                launch_gui()
            else:
                build_parser().print_help()
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/] [dim]Nothing was written.[/]")
        except (ImageStoreError, OSError, MemoryError) as e:
            render_error(e)


def launch_gui() -> int:
    """Open the optional PyQt6 interface (imagestore_gui/, next to this file). Only ever
    called on request: `-g/--gui` or `g` in the menu."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    # run as a script this module is __main__; let the GUI import the same module object
    sys.modules.setdefault("imagestore", sys.modules[__name__])
    try:
        import imagestore_gui
    except ImportError as e:
        render_error(ImageStoreError(
            f"the graphical interface is not available ({e})", "gui"))
        err_console.print("  [dim]It needs PyQt6: pip install -r requirements-gui.txt[/]")
        return 1
    return imagestore_gui.run()


def _window_mib(text: str) -> int:
    try:
        mib = int(text)
    except ValueError:
        mib = 0
    if not 1 <= mib <= LZMA_WINDOW_LIMIT >> 20:
        raise argparse.ArgumentTypeError("must be a whole number of MiB from 1 to 1024")
    return mib << 20


def _ecc_percent(text: str) -> float:
    try:
        pct = float(text)
    except ValueError:
        pct = -1
    if not 0 <= pct <= 50:
        raise argparse.ArgumentTypeError("must be a percentage from 0 to 50")
    return pct


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="imagestore", description=f"Serialize files into RGBA PNG images. {TAGLINE}",
                                 epilog="Run without arguments in a terminal for the interactive menu.")
    ap.add_argument("-g", "--gui", action="store_true", help="open the graphical interface (needs PyQt6)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("encode", help="file -> PNG")
    p.add_argument("file", type=Path, nargs="?", help="file or folder to encode (or use -t)")
    p.add_argument("-t", "--text", help="encode this string instead of a file")
    p.add_argument("-o", "--output", type=Path, help="output PNG (default: <file>.png, or text.png)")
    p.add_argument("--level", choices=LEVELS, default=DEFAULT_LEVEL,
                   help="compression: fast and balanced (default) use zstd on every core, max uses LZMA on "
                        "every core for the smallest image (several times slower)")
    p.add_argument("--no-compress", action="store_true", help="skip pre-compression")
    p.add_argument("--ecc", type=_ecc_percent, default=0, metavar="PERCENT",
                   help="add error correction using about this much extra space (0-50, default 0 = off): the "
                        "image can then be repaired after about half that share of it is damaged. Slower to "
                        "encode and decode")
    p.add_argument("--window", type=_window_mib, default=LZMA_WINDOW, metavar="MiB",
                   help="cap on the compression window, 1-1024 MiB (default 64; rounded up to a power of two; "
                        "--level max needs about 11x this in RAM per core used). Larger only helps when repeats "
                        "are far apart")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output")
    p.add_argument("--no-verify", action="store_true",
                   help="skip decoding the written PNG to confirm it (faster, not recommended)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--auto-ignore", action="store_true",
                   help="folders: skip .DS_Store, node_modules, __pycache__, .git, ... (default: ask)")
    g.add_argument("--no-auto-ignore", action="store_true", help="folders: include everything, don't ask")

    p = sub.add_parser("decode", help="PNG -> original file")
    p.add_argument("image", type=Path)
    p.add_argument("-o", "--output-dir", type=Path, help="directory to restore into (default: next to image)")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output")

    p = sub.add_parser("info", help="show metadata and verify integrity (exit 1 if it fails)")
    p.add_argument("image", type=Path)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        if args.gui:
            if args.cmd:
                ap.error("-g/--gui cannot be combined with a command")
            return launch_gui()
        if args.cmd == "encode":
            if (args.file is None) == (args.text is None):
                ap.error("encode needs exactly one of FILE or -t TEXT")
            if args.text is not None:
                do_encode_text(args.text, args.output, not args.no_compress, args.force, not args.no_verify,
                                 window=args.window, ecc=args.ecc, level=args.level)
            else:
                # ask only when someone is there to answer; scripts get exact, unfiltered folders
                ignore = True if args.auto_ignore else False if args.no_auto_ignore else (
                    None if sys.stdin.isatty() else False)
                do_encode(args.file, args.output, not args.no_compress, args.force, ignore, not args.no_verify,
                          window=args.window, ecc=args.ecc, level=args.level)
        elif args.cmd == "decode":
            do_decode(args.image, args.output_dir, args.force)
        elif args.cmd == "info":
            return 0 if do_info(args.image).ok else 1
        elif sys.stdin.isatty():
            interactive()
        else:
            ap.print_help()
    except (ImageStoreError, OSError, MemoryError) as e:
        render_error(e)
        return 1
    except (KeyboardInterrupt, EOFError):
        console.print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
