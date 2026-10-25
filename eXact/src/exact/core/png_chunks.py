"""Low-level PNG chunk parsing and writing.

eXact reads the chunk structure itself (instead of trusting a decoder's summary) so that
it can:

* verify the bit depth / colour type *before* any decoder silently converts samples
  (Pillow, for example, truncates 16-bit RGB to 8-bit "RGB");
* carry ancillary chunks (text, ICC profile, pHYs, ...) through a save byte-for-byte;
* report exactly which chunks exist, and which could not be preserved.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field
from typing import BinaryIO

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

COLOR_TYPE_NAMES = {
    0: "Grayscale",
    2: "Truecolor (RGB)",
    3: "Indexed-color (palette)",
    4: "Grayscale + alpha",
    6: "Truecolor + alpha (RGBA)",
}

# Ancillary chunk types whose meaning eXact understands well enough to decide whether
# they survive an edit. Everything here is kept unless a rule in png_io says otherwise.
KNOWN_ANCILLARY = {
    b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"cICP", b"mDCV", b"cLLI",
    b"tEXt", b"zTXt", b"iTXt", b"bKGD", b"hIST", b"pHYs", b"sPLT", b"eXIf", b"tIME",
}
APNG_CHUNKS = {b"acTL", b"fcTL", b"fdAT"}


class PngFormatError(ValueError):
    """The file is not a structurally valid PNG."""


@dataclass(frozen=True)
class Chunk:
    type: bytes
    data: bytes

    @property
    def name(self) -> str:
        return self.type.decode("latin-1")

    @property
    def is_critical(self) -> bool:
        return not (self.type[0] & 0x20)

    @property
    def is_safe_to_copy(self) -> bool:
        return bool(self.type[3] & 0x20)


@dataclass(frozen=True)
class IHDR:
    width: int
    height: int
    bit_depth: int
    color_type: int
    compression: int = 0
    filter_method: int = 0
    interlace: int = 0

    @classmethod
    def parse(cls, data: bytes) -> "IHDR":
        if len(data) != 13:
            raise PngFormatError(f"IHDR has length {len(data)}, expected 13")
        return cls(*struct.unpack(">IIBBBBB", data))

    def to_bytes(self) -> bytes:
        return struct.pack(
            ">IIBBBBB", self.width, self.height, self.bit_depth, self.color_type,
            self.compression, self.filter_method, self.interlace,
        )

    @property
    def color_type_name(self) -> str:
        return COLOR_TYPE_NAMES.get(self.color_type, f"unknown ({self.color_type})")


@dataclass
class ChunkLayout:
    """Structure of a PNG file. IDAT payloads are not retained (only counted)."""

    ihdr: IHDR
    before_idat: list[Chunk] = field(default_factory=list)  # in file order, PLTE included
    after_idat: list[Chunk] = field(default_factory=list)
    sequence: list[tuple[str, int]] = field(default_factory=list)  # (type, length) of every chunk
    idat_count: int = 0
    idat_bytes: int = 0
    file_size: int = 0
    file_sha256: str = ""
    crc_errors: list[str] = field(default_factory=list)
    trailing_bytes: int = 0

    def has(self, ctype: bytes) -> bool:
        return any(c.type == ctype for c in self.before_idat + self.after_idat)

    def find(self, ctype: bytes) -> list[Chunk]:
        return [c for c in self.before_idat + self.after_idat if c.type == ctype]

    def summarized_sequence(self) -> list[tuple[str, int, int]]:
        """(type, count, total length) with consecutive runs of the same type collapsed."""
        out: list[tuple[str, int, int]] = []
        for name, length in self.sequence:
            if out and out[-1][0] == name:
                prev = out[-1]
                out[-1] = (name, prev[1] + 1, prev[2] + length)
            else:
                out.append((name, 1, length))
        return out


class _HashingReader:
    def __init__(self, fp: BinaryIO):
        self.fp = fp
        self.sha = hashlib.sha256()
        self.count = 0

    def read(self, n: int) -> bytes:
        data = self.fp.read(n)
        self.sha.update(data)
        self.count += len(data)
        return data

    def drain(self) -> int:
        extra = 0
        while True:
            block = self.read(1 << 20)
            if not block:
                return extra
            extra += len(block)


def read_chunk_layout(path: str) -> ChunkLayout:
    """Parse the chunk structure of *path*, validating CRCs and hashing the file bytes."""
    with open(path, "rb") as raw:
        r = _HashingReader(raw)
        if r.read(8) != PNG_SIGNATURE:
            raise PngFormatError("not a PNG file (bad signature)")
        layout: ChunkLayout | None = None
        seen_idat = False
        idat_finished = False
        seen_iend = False
        crc_errors: list[str] = []
        while True:
            header = r.read(8)
            if not header:
                break
            if len(header) < 8:
                raise PngFormatError("truncated chunk header")
            length, ctype = struct.unpack(">I4s", header)
            if length > 0x7FFFFFFF:
                raise PngFormatError(f"chunk {ctype!r} has invalid length {length}")
            data = r.read(length)
            crc_bytes = r.read(4)
            if len(data) < length or len(crc_bytes) < 4:
                raise PngFormatError(f"truncated chunk {ctype!r}")
            name = ctype.decode("latin-1")
            if (zlib.crc32(data, zlib.crc32(ctype)) & 0xFFFFFFFF) != struct.unpack(">I", crc_bytes)[0]:
                crc_errors.append(name)
            if layout is None:
                if ctype != b"IHDR":
                    raise PngFormatError("first chunk is not IHDR")
                layout = ChunkLayout(ihdr=IHDR.parse(data))
                layout.sequence.append((name, length))
                continue
            layout.sequence.append((name, length))
            if ctype == b"IHDR":
                raise PngFormatError("duplicate IHDR chunk")
            if ctype == b"IEND":
                seen_iend = True
                layout.trailing_bytes = r.drain()
                break
            if ctype == b"IDAT":
                if idat_finished:
                    raise PngFormatError("IDAT chunks are not consecutive")
                seen_idat = True
                layout.idat_count += 1
                layout.idat_bytes += length
                continue
            if seen_idat:
                idat_finished = True
                layout.after_idat.append(Chunk(ctype, data))
            else:
                layout.before_idat.append(Chunk(ctype, data))
        if layout is None:
            raise PngFormatError("empty PNG (no IHDR)")
        if not seen_idat:
            raise PngFormatError("PNG contains no IDAT chunk")
        if not seen_iend:
            raise PngFormatError("PNG is truncated (no IEND chunk)")
        layout.crc_errors = crc_errors
        layout.file_size = r.count
        layout.file_sha256 = r.sha.hexdigest()
        return layout


def write_chunk(fp: BinaryIO, ctype: bytes, data: bytes) -> int:
    crc = zlib.crc32(data, zlib.crc32(ctype)) & 0xFFFFFFFF
    fp.write(struct.pack(">I4s", len(data), ctype))
    fp.write(data)
    fp.write(struct.pack(">I", crc))
    return 12 + len(data)


def file_sha256(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()
