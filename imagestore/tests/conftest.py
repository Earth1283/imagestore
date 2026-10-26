"""Shared setup for the ImageStore test suite.

imagestore.py sits one level above this folder. Putting that folder first on sys.path makes
`import imagestore` load the script itself, never a namespace package with the same name.
"""
from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

PKG_DIR = Path(__file__).resolve().parent.parent
if sys.path[0] != str(PKG_DIR):
    sys.path.insert(0, str(PKG_DIR))

import imagestore  # noqa: E402  (must come after the sys.path change)

# Files and folders that auto-ignore should drop, and files it should keep.
KEEP_FILES = {
    "a.txt": b"alpha\n",
    "sub/b.bin": random.Random(2).randbytes(4096),
}
JUNK_FILES = {
    ".DS_Store": b"junk",
    "sub/.DS_Store": b"junk",
    "node_modules/x/y.js": b"module.exports = 1;\n",
    "node_modules/x/z.js": b"module.exports = 2;\n",
    "node_modules/x/__pycache__/q.pyc": b"\x00\x01",  # nested junk inside an ignored tree
    "__pycache__/m.pyc": b"\x00\x01",
}
EMPTY_DIRS = ("empty_dir",)


@pytest.fixture(scope="session")
def istore():
    """The imagestore module (the script at PKG_DIR/imagestore.py)."""
    return imagestore


@pytest.fixture
def rand_bytes():
    """rand_bytes(n, seed=1): n reproducible pseudo-random bytes."""
    def make(n: int, seed: int = 1) -> bytes:
        return random.Random(seed).randbytes(n)
    return make


@pytest.fixture
def regular_files():
    """regular_files(root) -> {posix relative path: bytes} for every regular file under root."""
    def collect(root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    return collect


@pytest.fixture
def tamper():
    """tamper(src, dst, offset, xor=0x01): re-save an RGBA PNG with one byte of pixel data changed.

    The byte offset is into the RGBA buffer (row-major, four bytes per pixel), which is the
    same order the encoder writes the archive in.
    """
    def flip(src, dst, offset: int, xor: int = 0x01) -> Path:
        with Image.open(src) as im:
            im.load()
            buf = bytearray(im.tobytes())
            size = im.size
        buf[offset] ^= xor
        Image.frombytes("RGBA", size, bytes(buf)).save(dst, format="PNG")
        return Path(dst)
    return flip


@dataclass
class Project:
    path: Path
    keep: dict[str, bytes] = field(default_factory=dict)
    junk: dict[str, bytes] = field(default_factory=dict)

    @property
    def all(self) -> dict[str, bytes]:
        return {**self.keep, **self.junk}


@pytest.fixture
def project(tmp_path) -> Project:
    """A small folder: real files, junk that auto-ignore drops, and an empty directory."""
    root = tmp_path / "proj"
    for files in (KEEP_FILES, JUNK_FILES):
        for rel, data in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    for d in EMPTY_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    return Project(root, dict(KEEP_FILES), dict(JUNK_FILES))
