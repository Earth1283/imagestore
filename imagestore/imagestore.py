#!/usr/bin/env python3
"""ImageStore - stash any file, folder or string inside an RGBA PNG, using all four channels (alpha too).

    imagestore.py                      interactive menu (in a terminal)
    imagestore.py -g | --gui           graphical interface (optional, needs PyQt6)
    imagestore.py encode FILE_OR_FOLDER [-o OUT] [--no-compress] [-f] [--no-verify]
    imagestore.py encode -t "some text" [-o OUT]
    imagestore.py decode IMAGE [-o DIR] [-f]
    imagestore.py info IMAGE
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import io
import math
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import threading
import zlib
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from PIL import Image
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

MAGIC = b"IMST"
VERSION = 2  # v2: SHA-256 digest replaced the v1 CRC32
FLAG_ZLIB = 0x01
FLAG_TEXT = 0x02  # payload is UTF-8 text: decode prints it instead of writing a file
FLAG_DIR = 0x04   # payload is an uncompressed tar of a folder
# magic, version, flags, original size, stored size, sha256 of original, name length
HEADER = struct.Struct(">4sBBQQ32sH")
TEXT_NAME = "text.txt"
# matched against entry names (never the root folder itself) when auto-ignore is on
IGNORE_PATTERNS = (".DS_Store", "._*", "Thumbs.db", "desktop.ini", ".Trashes", ".Spotlight-V100",
                   "node_modules", "__pycache__", "*.pyc", ".git", ".venv", "venv")
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
class Meta:
    name: str
    flags: int
    orig_size: int
    stored_size: int
    digest: bytes

    @property
    def compressed(self) -> bool:
        return bool(self.flags & FLAG_ZLIB)

    @property
    def is_text(self) -> bool:
        return bool(self.flags & FLAG_TEXT)

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & FLAG_DIR)


# --------------------------------------------------------------------------- core


def _spool() -> BinaryIO:
    return tempfile.SpooledTemporaryFile(max_size=SPOOL)


def encode(src: BinaryIO, name: str, compress: bool = True, kind: int = 0) -> tuple[Image.Image, Meta]:
    """Build the image from a seekable binary stream, holding only the pixel buffer in memory."""
    src.seek(0)
    sha, size = hashlib.sha256(), 0
    packed = co = None
    packed_size = 0
    if compress:
        packed, co = _spool(), zlib.compressobj(9)
    while chunk := src.read(CHUNK):
        sha.update(chunk)
        size += len(chunk)
        if co:
            packed_size += packed.write(co.compress(chunk))
    flags = kind
    if co:
        packed_size += packed.write(co.flush())
    if co and packed_size < size:
        flags |= FLAG_ZLIB
        payload, stored = packed, packed_size
    else:
        payload, stored = src, size

    name_b = name.encode("utf-8")
    meta = Meta(name, flags, size, stored, sha.digest())
    head = HEADER.pack(MAGIC, VERSION, flags, size, stored, meta.digest, len(name_b)) + name_b

    pixels = math.ceil((len(head) + stored) / BPP)
    width = math.ceil(math.sqrt(pixels))
    height = math.ceil(pixels / width)
    buf = bytearray(width * height * BPP)  # zero-filled, so the padding needs no extra pass
    buf[:len(head)] = head
    view, pos, end = memoryview(buf), len(head), len(head) + stored
    payload.seek(0)
    while pos < end:
        n = payload.readinto(view[pos:end])
        if not n:
            raise ImageStoreError("source changed size while it was being encoded", "source_changed")
        pos += n
    if packed:
        packed.close()
    # RGBA maps the buffer in place instead of copying it
    return Image.frombuffer("RGBA", (width, height), buf, "raw", "RGBA", 0, 1), meta


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


def _verified(meta: Meta, chunks: Iterator[bytes]) -> Iterator[bytes]:
    sha, n = hashlib.sha256(), 0
    for chunk in chunks:
        sha.update(chunk)
        n += len(chunk)
        yield chunk
    if n != meta.orig_size:
        raise ImageStoreError(f"size mismatch: expected {meta.orig_size} bytes, got {n}", "size")
    if not hmac.compare_digest(sha.digest(), meta.digest):
        raise ImageStoreError("SHA-256 mismatch: pixel data was altered (lossy re-encode or alpha premultiplication?)",
                             "sha")


def decode(img: Image.Image) -> tuple[Meta, Iterator[bytes]]:
    """Parse the header and return (meta, original bytes in chunks).

    The size and SHA-256 checks run when the iterator is exhausted and raise ImageStoreError, so
    consume it fully before trusting the data (write to scratch space, then commit).
    """
    px = _Pixels(img)
    head = px.read(HEADER.size)
    if len(head) < HEADER.size:
        raise ImageStoreError("image too small to contain an ImageStore header", "too_small")
    magic, version, flags, orig, stored, digest, name_len = HEADER.unpack(head)
    if magic != MAGIC:
        raise ImageStoreError("not an ImageStore image (bad magic)", "magic")
    if version != VERSION:
        raise ImageStoreError(f"unsupported ImageStore version {version}", "version")
    if HEADER.size + name_len + stored > px.total:
        raise ImageStoreError("image is truncated (payload extends past the pixel data)", "truncated")
    name = bytes(px.read(name_len)).decode("utf-8", errors="replace")
    meta = Meta(name, flags, orig, stored, digest)
    chunks = _payload(px, stored)
    return meta, _verified(meta, _inflate(chunks, orig) if meta.compressed else chunks)


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


def pack_dir(path: Path, ignore: bool = False, exclude: Collection[str] | None = None,
             on_entry: Callable[[tarfile.TarInfo], None] | None = None) -> tuple[BinaryIO, int]:
    """Tar a folder into scratch space (uncompressed: zlib/PNG handle that). Returns (tar stream, file count).

    ignore skips every entry auto-ignore matches; exclude skips exactly the listed paths
    (POSIX, relative to the folder, as in IgnoreHit.rel), for a user-reviewed selection.
    on_entry is called for each entry kept (progress reporting; it may raise to abort).
    """
    files = 0
    root = path.name or "folder"
    skip = frozenset(exclude or ())

    def scrub(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        nonlocal files
        if ignore and ti.name != root and is_ignored(PurePosixPath(ti.name).name):
            return None  # returning None for a directory skips its whole subtree
        if skip and ti.name != root and ti.name[len(root) + 1:] in skip:
            return None
        if on_entry is not None:
            on_entry(ti)
        files += ti.isfile()
        ti.uid = ti.gid = 0  # don't leak local user/group names into the image
        ti.uname = ti.gname = ""
        return ti

    tmp = _spool()
    with tarfile.open(fileobj=tmp, mode="w:") as tar:
        tar.add(path, arcname=root, filter=scrub)
    return tmp, files


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
                files = sum(m.isfile() for m in tar.getmembers())
                tar.extractall(dest, filter="data")
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

    @property
    def header_size(self) -> int:
        return HEADER.size + len(self.meta.name.encode("utf-8"))

    @property
    def capacity(self) -> int:
        return self.width * self.height * BPP

    @property
    def padding(self) -> int:
        return self.capacity - self.header_size - self.meta.stored_size

    @property
    def saved_fraction(self) -> float:
        """How much smaller the stored payload is than the original (0 when stored raw)."""
        m = self.meta
        return 1 - m.stored_size / m.orig_size if m.orig_size and m.compressed else 0.0


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
    got, chunks = decode(open_image(path))
    if (got.name, got.flags, got.orig_size, got.stored_size, got.digest) != (
            meta.name, meta.flags, meta.orig_size, meta.stored_size, meta.digest):
        raise ImageStoreError("the written PNG's header does not match what was encoded", "verify")
    for _ in task.track(chunks, "Verifying written PNG", meta.orig_size):
        pass


def encode_stream(data: BinaryIO, name: str, out: Path, *, compress: bool = True, force: bool = False,
                  kind: int = 0, source: str = "", task: Task | None = None, verify: bool = True,
                  files: int = 0, skipped: int = 0) -> EncodeResult:
    """Encode a seekable stream to *out*: written to a temporary sibling, verified by decoding
    it again, and only then moved into place. Nothing is left behind on failure or cancel."""
    task = task or Task()
    out = Path(out)
    _check_output(out, force)
    total = data.seek(0, os.SEEK_END)
    data.seek(0)
    img, meta = encode(_TrackedReader(data, task, total, compress), name, compress, kind)
    width, height = img.size
    task.phase("Writing PNG")
    tmp = out.with_name(f".{out.name}.imagestore-{os.getpid()}.part")
    try:
        img.save(tmp, format="PNG", compress_level=1)
        del img  # free the pixel buffer before verification decodes a second copy
        task.check()
        if verify:
            _verify_png(tmp, meta, task)
        task.check()
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return EncodeResult(out, meta, width, height, out.stat().st_size, verify, source or name, compress,
                        files, skipped)


def portable_name(name: str) -> str:
    """File names that are not valid UTF-8 (possible on Linux) cannot be stored as UTF-8;
    keep them readable with U+FFFD for the undecodable bytes. The content is unaffected."""
    try:
        name.encode("utf-8")
        return name
    except UnicodeEncodeError:
        return os.fsencode(name).decode("utf-8", errors="replace")


def encode_path(src: Path, out: Path | None = None, *, compress: bool = True, force: bool = False,
                ignore: bool = False, exclude: Collection[str] | None = None, task: Task | None = None,
                verify: bool = True) -> EncodeResult:
    task = task or Task()
    src = Path(src)
    if src.is_dir():
        src = src.resolve()
        out = Path(out) if out else default_png_path(src)
        _check_output(out, force)  # fail before packing a large folder
        task.phase("Packing folder")  # done counts entries here, not bytes
        # -1: auto-ignore was on, but counting its hits would mean walking them; not worth it
        skipped = len(exclude) if exclude else (-1 if ignore else 0)
        tar, files = pack_dir(src, ignore, exclude=exclude, on_entry=lambda _ti: task.advance(1))
        with tar:
            return encode_stream(tar, portable_name(src.name) or "folder", out, compress=compress, force=force, kind=FLAG_DIR,
                                 source=f"folder {src}", task=task, verify=verify, files=files, skipped=skipped)
    if src.is_file():
        out = Path(out) if out else default_png_path(src)
        _check_output(out, force)
        with src.open("rb") as f:
            return encode_stream(f, portable_name(src.name), out, compress=compress, force=force, source=str(src), task=task,
                                 verify=verify)
    raise ImageStoreError(f"no such file or folder: {src}", "missing")


def encode_text(text: str, out: Path | None = None, *, compress: bool = True, force: bool = False,
                task: Task | None = None, verify: bool = True) -> EncodeResult:
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ImageStoreError(f"the text cannot be encoded as UTF-8: {e}", "text") from e
    return encode_stream(io.BytesIO(data), TEXT_NAME, Path(out or "text.png"), compress=compress,
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
    meta, chunks = decode(img)
    width, height = img.size
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
        meta, chunks = decode(img)
    except Cancelled:
        raise
    except ImageStoreError as e:
        return InspectResult(**base, meta=None, ok=False, error=e)
    sha, n, error = hashlib.sha256(), 0, None
    try:
        for chunk in task.track(chunks, "Verifying payload", meta.orig_size):
            sha.update(chunk)
            n += len(chunk)
    except Cancelled:
        raise
    except ImageStoreError as e:
        error = e
    # the checks run after the last chunk, so for size/SHA failures the digest is complete
    complete = error is None or error.kind in ("sha", "size")
    return InspectResult(**base, meta=meta, ok=error is None, error=error,
                         computed_digest=sha.digest() if complete else None, decoded_size=n)


# ------------------------------------------------------------------------ errors


@dataclass
class Explanation:
    title: str
    hint: str
    detail: str


_EXPLAIN = {
    "magic": ("Not an ImageStore image",
              "This PNG does not start with an ImageStore header. It was made by something else, or its "
              "pixels were changed so much that the header is gone."),
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
             "The zlib stream inside the image could not be decompressed. Pixel values changed after "
             "encoding (lossy re-compression, colour management or alpha premultiplication). No output "
             "was written."),
    "size": ("Integrity check failed (size mismatch)",
             "The payload decoded to a different length than the header records, so the pixels were "
             "altered. No output was written."),
    "sha": ("Integrity check failed (SHA-256 mismatch)",
            "The payload decoded, but its checksum does not match the one stored at encode time. Some "
            "pixel values changed after encoding. Lossy re-encoding, alpha premultiplication and "
            "colour-profile conversion are the usual causes; chat apps, image hosts and many editors do "
            "this. No output was written; use an untouched copy of the PNG."),
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
        return f"{human(m.stored_size)}  [dim]zlib ·[/] [green]{r.saved_fraction:.1%} smaller[/]"
    if r.compress_requested:
        return f"{human(m.stored_size)}  [dim]raw · zlib would not have made it smaller[/]"
    return f"{human(m.stored_size)}  [dim]raw · compression off[/]"


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
            f"zlib · {1 - m.stored_size / m.orig_size:.1%} smaller" if m.compressed and m.orig_size else "raw")
         + "[/]"),
        ("Capacity", f"{human(r.capacity)} across {r.width * r.height:,} pixels  "
                     f"[dim]({used / r.capacity:.1%} used)[/]" if r.capacity else "0 B"),
        ("Format", f"[dim]v{VERSION} · flags 0x{m.flags:02x}"
                   f"{' (' + ', '.join(n for f, n in ((FLAG_ZLIB, 'zlib'), (FLAG_TEXT, 'text'), (FLAG_DIR, 'folder')) if m.flags & f) + ')' if m.flags else ''}[/]"),
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
              ignore: bool | None = None, verify: bool = True) -> EncodeResult:
    """ignore: True/False decides auto-ignore for folders; None asks."""
    exclude = None
    if ignore is None:
        ignore, exclude = ask_ignore(Path(src).resolve()) if Path(src).is_dir() else (False, None)
    with _CliProgress() as task:
        result = encode_path(src, out, compress=compress, force=force, ignore=ignore, exclude=exclude, task=task,
                             verify=verify)
    render_encode(result)
    return result


def do_encode_text(text: str, out: Path | None, compress: bool, force: bool, verify: bool = True) -> EncodeResult:
    with _CliProgress() as task:
        result = encode_text(text, out, compress=compress, force=force, task=task, verify=verify)
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
    console.print(f"[dim]  in {escape(show_path(here))}[/]")


def _ask_output(default: Path) -> tuple[Path, bool] | None:
    out = clean_path(Prompt.ask("Output PNG", default=show_path(default)))
    if out.exists() and not Confirm.ask(f"{escape(show_path(out))} exists. Overwrite?", default=False):
        return None
    return out, out.exists()


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
    compress = Confirm.ask("Compress with zlib first? [dim](kept only if it makes the payload smaller)[/]",
                           default=True)
    with _CliProgress() as task:
        result = encode_path(src, out, compress=compress, force=force, ignore=ignore, exclude=exclude, task=task)
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
    compress = Confirm.ask("Compress with zlib first?", default=True)
    do_encode_text(text, out, compress, force)


def _interactive_decode(here: Path) -> Path:
    src = browse(here, images_only=True)
    with console.status("Reading header…"):
        _, meta = read_header(src)
    console.print(f"[dim]Contains[/] {_kind_text(meta)} [dim]· {human(meta.orig_size)}"
                  f"{' · zlib' if meta.compressed else ''}[/]")
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="imagestore", description=f"Serialize files into RGBA PNG images. {TAGLINE}",
                                 epilog="Run without arguments in a terminal for the interactive menu.")
    ap.add_argument("-g", "--gui", action="store_true", help="open the graphical interface (needs PyQt6)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("encode", help="file -> PNG")
    p.add_argument("file", type=Path, nargs="?", help="file or folder to encode (or use -t)")
    p.add_argument("-t", "--text", help="encode this string instead of a file")
    p.add_argument("-o", "--output", type=Path, help="output PNG (default: <file>.png, or text.png)")
    p.add_argument("--no-compress", action="store_true", help="skip zlib pre-compression")
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
                do_encode_text(args.text, args.output, not args.no_compress, args.force, not args.no_verify)
            else:
                # ask only when someone is there to answer; scripts get exact, unfiltered folders
                ignore = True if args.auto_ignore else False if args.no_auto_ignore else (
                    None if sys.stdin.isatty() else False)
                do_encode(args.file, args.output, not args.no_compress, args.force, ignore, not args.no_verify)
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
