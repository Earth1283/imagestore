"""Compatibility with archives written by the ORIGINAL encoder (tests/fixtures/legacy, never modified)."""
from __future__ import annotations

import hashlib
import io
import json
import random
from pathlib import Path

import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "legacy"
PROJECT = HERE.parent
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
BY_PNG = {e["png"]: e for e in MANIFEST}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _pixels(png: Path) -> bytes:
    with Image.open(png) as im:
        im.load()
        return im.tobytes()


ALL = [pytest.param(e, id=e["png"]) for e in MANIFEST]
FILES = [pytest.param(e, id=e["png"]) for e in MANIFEST if e["kind"] in ("file", "text")]
FOLDERS = [pytest.param(e, id=e["png"]) for e in MANIFEST if e["kind"] == "dir"]


@pytest.mark.parametrize("entry", ALL)
def test_legacy_archive_inspects_ok_with_manifest_header(istore, entry):
    res = istore.inspect_path(FIXTURES / entry["png"])
    assert res.ok, res.error

    m = res.meta
    assert m.name == entry["name"]
    assert m.flags == entry["flags"]
    assert m.orig_size == entry["orig_size"]
    assert m.stored_size == entry["stored_size"]
    assert m.digest.hex() == entry["sha256"]
    assert (res.width, res.height) == (entry["width"], entry["height"])
    assert res.computed_digest.hex() == entry["sha256"]


@pytest.mark.parametrize("entry", ALL)
def test_legacy_pixels_match_original_encoder(entry):
    assert _sha(_pixels(FIXTURES / entry["png"])) == entry["pixel_sha256"]


@pytest.mark.parametrize("entry", FILES)
def test_legacy_file_restores_exact_content(istore, tmp_path, entry):
    res = istore.decode_path(FIXTURES / entry["png"], tmp_path, text_in_memory=False)

    assert res.out == tmp_path / entry["name"]
    assert _sha(res.out.read_bytes()) == entry["content_sha256"]


def test_legacy_text_decodes_in_memory(istore):
    entry = BY_PNG["text.png"]
    res = istore.decode_path(FIXTURES / entry["png"])

    assert res.out is None
    assert _sha(res.text.encode("utf-8")) == entry["content_sha256"]


@pytest.mark.parametrize("entry", FOLDERS)
def test_legacy_folder_restores_every_file(istore, tmp_path, entry):
    res = istore.decode_path(FIXTURES / entry["png"], tmp_path)
    root = tmp_path / "project"

    for f in entry["files"]:
        assert _sha((root / f["path"]).read_bytes()) == f["sha256"], f["path"]

    restored = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert restored == {f["path"] for f in entry["files"]}
    assert res.files == len(entry["files"])


# Original inputs for the fixtures whose content is known. Re-encoding them with the current
# encoder must give the identical pixel buffer, which proves the format is unchanged.
REENCODE = {
    "binary-random-raw.png": (lambda: random.Random(921).randbytes(8192), "random.bin", True, 0),
    "binary-nocompress.png": (lambda: (bytes(range(256)) * 12)[:3000], "ramp.bin", False, 0),
    "empty.png": (lambda: b"", "empty.dat", True, 0),
    "text.png": (lambda: "Code is Art.*\nHandle with care. — ünïcødé ✓\n".encode("utf-8"),
                 "text.txt", True, 2),
}


@pytest.mark.parametrize("png_name", list(REENCODE))
def test_reencode_is_pixel_identical_to_original(istore, png_name):
    make, name, compress, kind = REENCODE[png_name]
    entry = BY_PNG[png_name]
    assert kind == entry["flags"]  # the table's kind is the manifest's flag bits (text = 0x02)

    img, meta = istore.encode(io.BytesIO(make()), name, compress, kind)

    assert _sha(img.tobytes()) == entry["pixel_sha256"]
    assert (img.width, img.height) == (entry["width"], entry["height"])
    assert meta.flags == entry["flags"]
    assert meta.stored_size == entry["stored_size"]
    assert meta.digest.hex() == entry["sha256"]


def test_bundled_sample_inspects_ok(istore):
    sample = PROJECT / "imagestore.py.png"
    assert istore.inspect_path(sample).ok is True
