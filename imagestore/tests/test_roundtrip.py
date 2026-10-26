"""Encode -> decode round trips through the operations API (encode_path, decode_path, encode_text)."""
from __future__ import annotations

import hashlib
import os
import random
import tarfile

import pytest

from imagestore import ImageStoreError

COMPRESSIBLE_200K = b"abcdefgh" * (200 * 1024 // 8)  # 204800 bytes of repeats
RANDOM_300K = random.Random(300).randbytes(300 * 1024)


def _write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ----------------------------------------------------------------- single files

@pytest.mark.parametrize(
    "name,data,compress",
    [
        ("empty.dat", b"", True),
        ("one.bin", b"\x7f", True),
        ("compressible.txt", COMPRESSIBLE_200K, True),
        ("random.bin", RANDOM_300K, True),
        ("plain.txt", COMPRESSIBLE_200K, False),
        ("résumé ✓.txt", b"unicode file name\n", True),
    ],
    ids=["empty", "one-byte", "compressible-200k", "random-300k", "compress-off", "unicode-name"],
)
def test_file_round_trip_is_byte_identical(istore, tmp_path, name, data, compress):
    src = _write(tmp_path / "in" / name, data)
    png = tmp_path / "img.png"

    istore.encode_path(src, png, compress=compress)
    out_dir = tmp_path / "restored"
    dec = istore.decode_path(png, out_dir)

    assert dec.out == out_dir / name
    assert (out_dir / name).read_bytes() == data

def test_highly_compressible_file_is_stored_zlib(istore, tmp_path):
    src = _write(tmp_path / "c.txt", COMPRESSIBLE_200K)
    res = istore.encode_path(src, tmp_path / "c.png", compress=True)

    assert res.meta.compressed is True
    assert res.compress_requested is True
    assert res.saved_fraction > 0.9


def test_random_file_is_stored_raw_when_compress_requested(istore, tmp_path):
    src = _write(tmp_path / "r.bin", RANDOM_300K)
    res = istore.encode_path(src, tmp_path / "r.png", compress=True)

    assert res.meta.compressed is False  # zlib does not help random data, so it is skipped
    assert res.compress_requested is True
    assert res.meta.stored_size == len(RANDOM_300K)
    assert res.saved_fraction == 0.0


def test_compress_off_stores_raw(istore, tmp_path):
    src = _write(tmp_path / "c.txt", COMPRESSIBLE_200K)
    res = istore.encode_path(src, tmp_path / "c.png", compress=False)

    assert res.meta.compressed is False
    assert res.compress_requested is False
    assert res.meta.stored_size == res.meta.orig_size == len(COMPRESSIBLE_200K)


def test_multi_chunk_compressed_round_trip(istore, tmp_path):
    # 3 MiB spans several CHUNK-sized reads on both the encode and the streaming inflate path
    data = (b"multi-chunk payload " * 200_000)[: 3 << 20]
    src = _write(tmp_path / "big.txt", data)
    png = tmp_path / "big.png"
    res = istore.encode_path(src, png)
    assert res.meta.compressed is True

    dec = istore.decode_path(png, tmp_path / "out")
    assert dec.out.read_bytes() == data


def test_encode_result_fields(istore, tmp_path, rand_bytes):
    data = rand_bytes(5000, seed=4)
    src = _write(tmp_path / "blob.bin", data)
    png = tmp_path / "blob.png"

    res = istore.encode_path(src, png)

    assert res.out == png and png.exists()
    assert res.png_size == png.stat().st_size
    assert res.verified is True
    assert res.meta.orig_size == len(data)
    assert res.meta.digest == hashlib.sha256(data).digest()
    assert res.capacity == res.width * res.height * 4
    assert res.width * res.height * 4 >= res.header_size + res.meta.stored_size
    assert res.padding >= 0


# ---------------------------------------------------------------------- text

def test_text_round_trip_in_memory(istore, tmp_path):
    png = tmp_path / "t.png"
    res = istore.encode_text("Code is Art.* ✓", png)
    assert res.meta.is_text

    dec = istore.decode_path(png, text_in_memory=True)

    assert dec.text == "Code is Art.* ✓"
    assert dec.out is None


def test_text_round_trip_to_file(istore, tmp_path):
    png = tmp_path / "t.png"
    istore.encode_text("Code is Art.* ✓", png)
    out_dir = tmp_path / "out"

    dec = istore.decode_path(png, out_dir, text_in_memory=False)

    assert dec.out == out_dir / "text.txt"
    assert dec.out.read_text(encoding="utf-8") == "Code is Art.* ✓"


# -------------------------------------------------------------------- folders

def test_folder_round_trip_keeps_everything(istore, tmp_path, project, regular_files):
    png = tmp_path / "proj.png"

    res = istore.encode_path(project.path, png, ignore=False)
    assert res.meta.is_dir is True
    assert res.files == len(project.all)

    dest = tmp_path / "fresh"
    dec = istore.decode_path(png, dest)

    restored = dest / "proj"
    assert dec.files == len(project.all)
    assert regular_files(restored) == project.all
    assert (restored / "empty_dir").is_dir()


def test_folder_auto_ignore_drops_junk_only(istore, tmp_path, project, regular_files):
    png = tmp_path / "clean.png"

    res = istore.encode_path(project.path, png, ignore=True)
    assert res.skipped == -1  # auto-ignore on: counting the hits was skipped on purpose
    assert res.files == len(project.keep)

    dest = tmp_path / "fresh"
    istore.decode_path(png, dest)

    restored = dest / "proj"
    assert regular_files(restored) == project.keep
    assert (restored / "empty_dir").is_dir()
    assert not (restored / "node_modules").exists()
    assert not (restored / "__pycache__").exists()
    assert not (restored / ".DS_Store").exists()


def test_scan_ignored_detail_lists_hits(istore, project):
    hits = istore.scan_ignored_detail(project.path)
    by_rel = {h.rel: h for h in hits}

    # the ignored tree is reported once, not again for its nested __pycache__
    assert set(by_rel) == {".DS_Store", "sub/.DS_Store", "node_modules", "__pycache__"}

    nm = by_rel["node_modules"]
    assert nm.is_dir is True
    assert nm.files == 3
    assert nm.size == sum(len(v) for k, v in project.junk.items() if k.startswith("node_modules/"))

    ds = by_rel["sub/.DS_Store"]
    assert ds.is_dir is False
    assert ds.files == 1
    assert ds.size == len(b"junk")

    assert by_rel["__pycache__"].files == 1
    assert all(h.path == project.path / h.rel for h in hits)


def test_scan_ignored_totals_match_detail(istore, project):
    paths, files, size = istore.scan_ignored(project.path)
    hits = istore.scan_ignored_detail(project.path)

    assert sorted(paths) == sorted(h.path for h in hits)
    assert files == sum(h.files for h in hits) == len(project.junk)
    assert size == sum(h.size for h in hits) == sum(len(v) for v in project.junk.values())


def test_pack_dir_auto_ignore_equals_explicit_exclude(istore, project):
    auto, auto_files = istore.pack_dir(project.path, ignore=True)
    exclude = {h.rel for h in istore.scan_ignored_detail(project.path)}
    explicit, explicit_files = istore.pack_dir(project.path, ignore=False, exclude=exclude)

    auto.seek(0)
    explicit.seek(0)
    assert auto.read() == explicit.read()
    assert auto_files == explicit_files == len(project.keep)


def test_pack_dir_exclude_is_exact(istore, project):
    stream, _ = istore.pack_dir(project.path, ignore=False, exclude={"node_modules"})
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:") as tf:
        names = tf.getnames()

    assert not any(n.startswith("proj/node_modules") for n in names)
    assert "proj/.DS_Store" in names  # not excluded, so kept
    assert "proj/sub/.DS_Store" in names
    assert "proj/__pycache__/m.pyc" in names


# ------------------------------------------------------- overwrite and output rules

def test_decode_existing_file_needs_force(istore, tmp_path):
    src = _write(tmp_path / "note.txt", b"fresh")
    png = tmp_path / "note.png"
    istore.encode_path(src, png)
    out_dir = tmp_path / "out"
    _write(out_dir / "note.txt", b"old")

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, out_dir)
    assert ei.value.kind == "exists"
    assert (out_dir / "note.txt").read_bytes() == b"old"

    istore.decode_path(png, out_dir, force=True)
    assert (out_dir / "note.txt").read_bytes() == b"fresh"


def test_decode_existing_folder_needs_force_then_merges(istore, tmp_path, project, regular_files):
    png = tmp_path / "proj.png"
    istore.encode_path(project.path, png)
    dest = tmp_path / "dest"
    istore.decode_path(png, dest)

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, dest)
    assert ei.value.kind == "exists"

    istore.decode_path(png, dest, force=True)
    assert regular_files(dest / "proj") == project.all


def test_encode_existing_output_without_force_leaves_it_alone(istore, tmp_path):
    src = _write(tmp_path / "a.bin", b"payload")
    png = tmp_path / "a.png"
    png.write_bytes(b"not a png, keep me")

    with pytest.raises(ImageStoreError) as ei:
        istore.encode_path(src, png)
    assert ei.value.kind == "exists"
    assert png.read_bytes() == b"not a png, keep me"
    assert not any(".part" in p.name for p in tmp_path.iterdir())

    istore.encode_path(src, png, force=True)
    assert istore.decode_path(png, tmp_path / "out").out.read_bytes() == b"payload"


def test_encode_into_missing_folder(istore, tmp_path):
    src = _write(tmp_path / "a.bin", b"payload")

    with pytest.raises(ImageStoreError) as ei:
        istore.encode_path(src, tmp_path / "no-such-dir" / "a.png")
    assert ei.value.kind == "missing"
    assert not any(".part" in p.name for p in tmp_path.rglob("*"))


# ----------------------------------------------------------------- known defects

def test_non_utf8_file_name_round_trips(istore, tmp_path):
    name = os.fsdecode(b"caf\xe9.txt")  # Latin-1 bytes in a Linux file name
    data = b"legacy name\n"
    src = _write(tmp_path / "in" / name, data)
    png = tmp_path / "legacy.png"

    istore.encode_path(src, png)
    out_dir = tmp_path / "restored"
    istore.decode_path(png, out_dir)

    # the content is exact; the stored name keeps the undecodable byte as U+FFFD
    assert (out_dir / "caf\ufffd.txt").read_bytes() == data


def test_text_with_lone_surrogate_is_a_clear_error(istore, tmp_path):
    with pytest.raises(istore.ImageStoreError) as ei:
        istore.encode_text("bad\udcff", tmp_path / "t.png")
    assert ei.value.kind == "text"
    assert not (tmp_path / "t.png").exists()
