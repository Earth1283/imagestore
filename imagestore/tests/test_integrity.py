"""Integrity checks: tampered, corrupt and foreign images are refused, and no output appears."""
from __future__ import annotations

import errno
import hashlib
import io
import os
import random
import tarfile

import pytest
from PIL import Image

from imagestore import ImageStoreError, main as cli_main


def _no_output(folder) -> bool:
    return not folder.exists() or not any(folder.iterdir())


@pytest.fixture
def tampered_archive(istore, tmp_path, rand_bytes, tamper):
    """A 5000-byte uncompressed archive whose pixel data has one byte flipped at offset 200."""
    data = rand_bytes(5000, seed=7)
    src = tmp_path / "src" / "blob.bin"  # kept apart so the restored name below is free
    src.parent.mkdir()
    src.write_bytes(data)
    png = tmp_path / "blob.png"
    istore.encode_path(src, png, compress=False)
    bad = tamper(png, tmp_path / "bad.png", offset=200)
    return {"data": data, "good": png, "bad": bad}


# ------------------------------------------------------------------- tampering

def test_tampered_payload_is_refused_and_writes_nothing(istore, tmp_path, tampered_archive):
    out_dir = tmp_path / "out"
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(tampered_archive["bad"], out_dir)
    assert ei.value.kind == "sha"
    assert _no_output(out_dir)

    # and with the default location (next to the image), where the restored name would land
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(tampered_archive["bad"])
    assert ei.value.kind == "sha"
    assert not (tmp_path / "blob.bin").exists()


def test_tampered_payload_inspect_reports_digest_mismatch(istore, tampered_archive):
    res = istore.inspect_path(tampered_archive["bad"])

    assert res.ok is False
    assert res.error.kind == "sha"
    assert res.meta.digest == hashlib.sha256(tampered_archive["data"]).digest()
    assert res.computed_digest is not None
    assert res.computed_digest != res.meta.digest


def test_tampered_payload_cli_exit_codes(istore, tmp_path, tampered_archive, capsys):
    bad = str(tampered_archive["bad"])
    assert cli_main(["info", bad]) == 1

    out_dir = tmp_path / "cli-out"
    assert cli_main(["decode", bad, "-o", str(out_dir)]) == 1
    assert _no_output(out_dir)
    assert "error:" in capsys.readouterr().err


def test_good_archive_still_passes(istore, tampered_archive):
    assert istore.inspect_path(tampered_archive["good"]).ok is True


def test_corrupt_zlib_payload_is_detected(istore, tmp_path, tamper):
    name = "compressible.txt"
    src = tmp_path / name
    src.write_bytes(b"compress me please " * 2000)
    png = tmp_path / "c.png"
    res = istore.encode_path(src, png, compress=True)
    assert res.meta.compressed is True

    offset = istore.HEADER.size + len(name.encode("utf-8")) + 10  # inside the deflate stream
    bad = tamper(png, tmp_path / "bad.png", offset=offset, xor=0xFF)

    out_dir = tmp_path / "out"
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(bad, out_dir)
    assert ei.value.kind in {"zlib", "sha", "size"}
    assert _no_output(out_dir)


def test_version_byte_mismatch_is_reported(istore, tmp_path, rand_bytes, tamper):
    src = tmp_path / "v.bin"
    src.write_bytes(rand_bytes(300, seed=9))
    png = tmp_path / "v.png"
    istore.encode_path(src, png)
    bad = tamper(png, tmp_path / "v-bad.png", offset=4)  # version byte: 2 -> 3

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(bad, tmp_path / "out")
    assert ei.value.kind == "version"


# ------------------------------------------------------------ foreign images

def test_random_rgba_png_has_bad_magic(istore, tmp_path):
    png = tmp_path / "random.png"
    Image.frombytes("RGBA", (64, 64), random.Random(3).randbytes(64 * 64 * 4)).save(png)

    res = istore.inspect_path(png)
    assert res.ok is False
    assert res.meta is None
    assert res.error.kind == "magic"

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, tmp_path / "out")
    assert ei.value.kind == "magic"


def test_rgb_image_is_refused_as_mode_error(istore, tmp_path, rand_bytes):
    src = tmp_path / "x.bin"
    src.write_bytes(rand_bytes(400, seed=5))
    png = tmp_path / "x.png"
    istore.encode_path(src, png)
    rgb = tmp_path / "x-rgb.png"
    with Image.open(png) as im:
        im.convert("RGB").save(rgb)  # alpha stripped, the usual chat-app damage

    res = istore.inspect_path(rgb)
    assert res.ok is False and res.meta is None
    assert res.error.kind == "mode"

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(rgb, tmp_path / "out")
    assert ei.value.kind == "mode"


def test_one_pixel_image_is_too_small(istore, tmp_path):
    png = tmp_path / "tiny.png"
    Image.new("RGBA", (1, 1), (1, 2, 3, 4)).save(png)

    res = istore.inspect_path(png)
    assert res.ok is False
    assert res.error.kind == "too_small"

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, tmp_path / "out")
    assert ei.value.kind == "too_small"


def test_missing_input_is_an_open_error_with_not_found_explanation(istore, tmp_path):
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(tmp_path / "missing.png")
    assert ei.value.kind == "open"
    assert istore.explain(ei.value).title == "Not found"


# ------------------------------------------------------ hostile folder archives

def test_archive_path_traversal_is_refused(istore, tmp_path):
    # A folder archive whose first member climbs out of the destination with "..".
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        evil = tarfile.TarInfo("../escape.txt")
        evil.size = 5
        tar.addfile(evil, io.BytesIO(b"pwned"))
        ok = tarfile.TarInfo("proj/ok.txt")
        ok.size = 2
        tar.addfile(ok, io.BytesIO(b"ok"))
    buf.seek(0)
    png = tmp_path / "evil.png"
    istore.encode_stream(buf, "proj", png, kind=istore.FLAG_DIR)

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, dest)
    assert ei.value.kind == "extract"
    assert not (tmp_path / "escape.txt").exists()


def test_round_trip_survives_pillow_decompression_bomb_limit(istore, tmp_path, rand_bytes, monkeypatch):
    # Pillow's default limit is about 89M pixels (error at twice that), roughly 715 MB of payload.
    # The limit is lowered here so the same condition shows up with a small file.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 500)
    # open_image lifts the guard to MAX_ARCHIVE_PIXELS while reading; lower that too to prove
    # it still applies a (larger) limit and restores Pillow's afterwards
    monkeypatch.setattr(istore, "MAX_ARCHIVE_PIXELS", 10_000)
    data = rand_bytes(5000, seed=12)
    src = tmp_path / "blob.bin"
    src.write_bytes(data)
    png = tmp_path / "blob.png"

    istore.encode_path(src, png)
    dec = istore.decode_path(png, tmp_path / "out")
    assert dec.out.read_bytes() == data
    assert Image.MAX_IMAGE_PIXELS == 500
    big = tmp_path / "big.bin"
    big.write_bytes(rand_bytes(100_000, seed=13))  # ~25,000 px: past 2x the 10,000 limit, where Pillow errors
    with pytest.raises(ImageStoreError) as ei:
        istore.encode_path(big, tmp_path / "big.png")
    assert ei.value.kind == "open"
    assert not (tmp_path / "big.png").exists()


def test_failed_folder_restore_leaves_nothing_behind(istore, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("alpha")
    os.symlink("/etc/hostname", proj / "link")  # absolute symlink: the 'data' filter refuses it
    png = tmp_path / "proj.png"
    istore.encode_path(proj, png, compress=False)

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, dest)
    assert ei.value.kind == "extract"
    assert list(dest.rglob("*")) == []
    # a destination the decode had to create is removed again
    with pytest.raises(ImageStoreError):
        istore.decode_path(png, tmp_path / "fresh")
    assert not (tmp_path / "fresh").exists()


# --------------------------------------------------------------------- explain()

KINDS = ["magic", "mode", "too_small", "version", "truncated", "zlib", "squeeze", "memory", "ecc", "size", "sha", "verify",
         "extract", "exists", "missing", "source_changed", "cancelled", "open", "text"]


@pytest.mark.parametrize("kind", KINDS)
def test_explain_known_kinds_have_title_and_hint(istore, kind):
    ex = istore.explain(ImageStoreError("detail text", kind))
    assert isinstance(ex, istore.Explanation)
    assert ex.title
    assert ex.hint
    assert ex.detail == "detail text"


def test_explain_unknown_kind_falls_back(istore):
    ex = istore.explain(ImageStoreError("x", "something_new"))
    assert ex.title == "ImageStore error"


def test_explain_disk_full(istore):
    ex = istore.explain(OSError(errno.ENOSPC, "x"))
    assert "disk space" in ex.title


def test_explain_permission_denied(istore):
    ex = istore.explain(PermissionError(errno.EACCES, "x", "f"))
    assert ex.title == "Permission denied"


def test_explain_memory_error(istore):
    ex = istore.explain(MemoryError())
    assert ex.title == "Not enough memory"
