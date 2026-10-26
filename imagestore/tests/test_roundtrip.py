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


# ---------------------------------------------------------- folder zero padding

def _nested_tree(root, rand_bytes):
    """Many small files in nested folders: tar pads each one to a 512-byte block."""
    files = {}
    for d in range(6):
        for s in range(3):
            for f in range(3):
                rel = f"d{d}/s{s}/f{f}.bin"
                files[rel] = rand_bytes(100 + 37 * (d + s + f), seed=d * 100 + s * 10 + f)
                _write(root / rel, files[rel])
    return files


def _empty_pixel_fraction(png) -> float:
    from PIL import Image
    with Image.open(png) as im:
        buf = im.tobytes()
    zero = sum(buf[i:i + 4] == b"\0\0\0\0" for i in range(0, len(buf), 4))
    return zero / (len(buf) // 4)


@pytest.mark.parametrize("compress", [False, True], ids=["compress-off", "compress-on"])
def test_nested_folder_leaves_almost_no_empty_pixels(istore, tmp_path, rand_bytes, regular_files, compress):
    proj = tmp_path / "proj"
    files = _nested_tree(proj, rand_bytes)
    png = tmp_path / "proj.png"

    res = istore.encode_path(proj, png, compress=compress)

    assert res.meta.compressed or res.meta.squeezed
    assert res.meta.stored_size < res.meta.orig_size / 2  # tar is mostly padding here
    assert _empty_pixel_fraction(png) < 0.01
    out = tmp_path / "out"
    istore.decode_path(png, out)
    assert regular_files(out / "proj") == files


def test_squeezed_folder_info_and_flags(istore, tmp_path, rand_bytes):
    proj = tmp_path / "proj"
    _nested_tree(proj, rand_bytes)
    png = tmp_path / "proj.png"
    istore.encode_path(proj, png, compress=False)

    info = istore.inspect_path(png)

    assert info.ok
    assert info.meta.squeezed and not info.meta.compressed
    assert info.meta.flags == istore.FLAG_DIR | istore.FLAG_SQUEEZE


def test_plain_files_are_never_squeezed(istore, tmp_path):
    src = _write(tmp_path / "zeros.bin", b"\0" * 50_000)
    res = istore.encode_path(src, tmp_path / "z.png", compress=False)
    assert res.meta.flags == 0 and res.meta.stored_size == 50_000


@pytest.mark.parametrize("seed", range(8))
def test_squeeze_round_trips_across_arbitrary_chunk_boundaries(istore, seed):
    rng = random.Random(seed)
    parts = []
    for _ in range(60):
        parts.append(b"\0" * rng.choice([0, 1, 7, 8, 9, 300, 70_000]))
        parts.append(rng.randbytes(rng.choice([0, 1, 5, 200, 4000])))
    data = b"".join(parts)
    packed = b"".join(istore.squeeze(data[i:i + 50_000]) for i in range(0, len(data), 50_000))
    cuts = sorted(rng.sample(range(len(packed)), min(40, len(packed))))
    pieces = [packed[a:b] for a, b in zip([0] + cuts, cuts + [len(packed)])]  # splits mid-varint, mid-literal

    assert b"".join(istore._unsqueeze(iter(pieces), len(data))) == data


def test_unsqueeze_output_is_capped_at_declared_size(istore):
    bomb = istore._varint(10 ** 12) + istore._varint(0)  # claims a terabyte of zeros
    out = b"".join(istore._unsqueeze(iter([bomb]), 1000))
    assert out == b"\0" * 1000


def test_malformed_squeezed_payload_is_reported(istore):
    junk = bytes([0xFF] * 12)  # a length that never ends
    with pytest.raises(ImageStoreError) as ei:
        b"".join(istore._unsqueeze(iter([junk]), 100))
    assert ei.value.kind == "squeeze"


def test_tampered_squeezed_archive_is_refused(istore, tmp_path, rand_bytes, tamper):
    proj = tmp_path / "proj"
    _nested_tree(proj, rand_bytes)
    png = tmp_path / "proj.png"
    istore.encode_path(proj, png, compress=False)
    bad = tamper(png, tmp_path / "bad.png", offset=2000)
    out = tmp_path / "out"

    with pytest.raises(ImageStoreError):
        istore.decode_path(bad, out)

    assert not out.exists()


# ------------------------------------------------------------------------ LZMA

def _text_blob() -> bytes:
    rng = random.Random(5)
    words = [rng.randbytes(rng.randint(2, 9)).hex() for _ in range(3000)]
    return " ".join(rng.choice(words) for _ in range(60_000)).encode()


def test_lzma_beats_zlib_on_text_and_round_trips(istore, tmp_path):
    import zlib
    data = _text_blob()
    src = _write(tmp_path / "t.txt", data)
    png = tmp_path / "t.png"

    res = istore.encode_path(src, png)

    assert res.meta.method == "lzma"
    assert res.meta.stored_size < len(zlib.compress(data, 9))
    assert istore.decode_path(png, tmp_path / "out").out.read_bytes() == data


@pytest.mark.parametrize("seed", range(4))
def test_unlzma_survives_arbitrary_chunk_boundaries(istore, seed):
    import lzma
    rng = random.Random(seed)
    data = _text_blob()[:300_000]
    window = istore.lzma_window(len(data))
    co = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=istore.lzma_filters(window))
    packed = bytes([window.bit_length() - 1]) + co.compress(data) + co.flush()
    cuts = sorted(rng.sample(range(len(packed)), 30))
    pieces = [packed[a:b] for a, b in zip([0] + cuts, cuts + [len(packed)])]

    assert b"".join(istore._unlzma(iter(pieces), len(data))) == data


def test_tampered_lzma_archive_is_refused(istore, tmp_path, tamper):
    src = _write(tmp_path / "t.txt", _text_blob())
    png = tmp_path / "t.png"
    istore.encode_path(src, png)
    bad = tamper(png, tmp_path / "bad.png", offset=500)

    with pytest.raises(ImageStoreError):
        istore.decode_path(bad, tmp_path / "out")

    assert not (tmp_path / "out").exists()


def test_conflicting_payload_flags_are_rejected(istore, tmp_path):
    src = _write(tmp_path / "t.txt", b"hello world " * 100)
    png = tmp_path / "t.png"
    istore.encode_path(src, png, compress=False)
    from PIL import Image
    with Image.open(png) as im:
        buf = bytearray(im.tobytes())
        size = im.size
    buf[5] |= istore.FLAG_ZLIB | istore.FLAG_LZMA  # flags byte sits after magic and version
    Image.frombytes("RGBA", size, bytes(buf)).save(png)

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, tmp_path / "out")
    assert ei.value.kind == "version"


# ------------------------------------------------------------------ duplicate files

def _dup_tree(root, rand_bytes):
    blob = rand_bytes(40_000, seed=77)
    files = {"a/big.bin": blob, "b/c/copy.bin": blob, "d/again.bin": blob, "uniq.bin": rand_bytes(5000, seed=78),
             "a/same_size.bin": rand_bytes(40_000, seed=79)}  # same size as blob, different content
    for rel, data in files.items():
        _write(root / rel, data)
    os.utime(root / "d/again.bin", (1_600_000_000, 1_600_000_000))
    os.chmod(root / "b/c/copy.bin", 0o600)
    return files


def test_duplicate_files_are_stored_once_and_restored_as_independent_copies(istore, tmp_path, rand_bytes, regular_files):
    proj = tmp_path / "proj"
    files = _dup_tree(proj, rand_bytes)
    png = tmp_path / "proj.png"

    res = istore.encode_path(proj, png, compress=False)

    assert res.meta.stored_size < 2 * 40_000 + 5000 + 20_000  # two blobs, not four
    assert res.files == len(files)
    out = tmp_path / "out"
    dec = istore.decode_path(png, out)
    assert dec.files == len(files)
    assert regular_files(out / "proj") == files
    first, copy = out / "proj/a/big.bin", out / "proj/b/c/copy.bin"
    assert first.stat().st_ino != copy.stat().st_ino and copy.stat().st_nlink == 1
    assert copy.stat().st_mode & 0o777 == 0o600
    assert (out / "proj/d/again.bin").stat().st_mtime == 1_600_000_000
    copy.write_bytes(b"edited")
    assert first.read_bytes() == files["a/big.bin"]


def test_duplicate_of_an_ignored_file_keeps_its_data(istore, tmp_path, rand_bytes, regular_files):
    proj = tmp_path / "proj"
    blob = rand_bytes(3000, seed=5)
    _write(proj / "__pycache__" / "x.bin", blob)  # ignored, and walked before "z.bin"
    _write(proj / "z.bin", blob)
    png = tmp_path / "p.png"

    istore.encode_path(proj, png, ignore=True, compress=False)

    istore.decode_path(png, tmp_path / "out")
    assert regular_files(tmp_path / "out" / "proj") == {"z.bin": blob}


def test_empty_files_are_not_pointers(istore, tmp_path):
    proj = tmp_path / "proj"
    for n in ("a", "b"):
        _write(proj / n, b"")
    png = tmp_path / "p.png"
    istore.encode_path(proj, png, compress=False)
    istore.decode_path(png, tmp_path / "out")
    assert (tmp_path / "out/proj/a").read_bytes() == b"" and (tmp_path / "out/proj/b").exists()


@pytest.mark.parametrize("target", ["/etc/hostname", "../outside", "missing.txt", "sub"])
def test_hostile_pointer_is_refused_and_writes_nothing(istore, tmp_path, target):
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        d = tarfile.TarInfo("proj"); d.type = tarfile.DIRTYPE; tar.addfile(d)
        d = tarfile.TarInfo("proj/sub"); d.type = tarfile.DIRTYPE; tar.addfile(d)
        ln = tarfile.TarInfo("proj/evil"); ln.type = tarfile.LNKTYPE; ln.linkname = target; tar.addfile(ln)
    png = tmp_path / "evil.png"
    istore.encode_stream(buf, "proj", png, kind=istore.FLAG_DIR)
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, dest)

    assert ei.value.kind == "extract"
    assert list(dest.rglob("*")) == []


def test_encode_result_reports_duplicates(istore, tmp_path, rand_bytes):
    proj = tmp_path / "proj"
    _dup_tree(proj, rand_bytes)

    res = istore.encode_path(proj, tmp_path / "p.png", compress=False)

    assert (res.duplicates, res.duplicate_bytes) == (2, 80_000)


def test_encode_result_without_duplicates_reports_none(istore, tmp_path, rand_bytes):
    _write(tmp_path / "proj" / "a.bin", rand_bytes(3000, seed=1))
    res = istore.encode_path(tmp_path / "proj", tmp_path / "p.png")
    assert (res.duplicates, res.duplicate_bytes) == (0, 0)


@pytest.mark.parametrize("size,window", [(0, 4096), (1, 4096), (4096, 4096), (4097, 8192), (3 << 20, 4 << 20),
                                         (5 << 30, 64 << 20)])
def test_lzma_window_follows_the_original_size(istore, size, window):
    assert istore.lzma_window(size) == window


@pytest.mark.parametrize("cap,size,window", [(1 << 20, 50 << 20, 1 << 20), (3 << 20, 50 << 20, 4 << 20),
                                             (1, 50 << 20, 4096), (1 << 40, 5 << 30, 1 << 30),
                                             (1 << 30, 1000, 4096)])
def test_lzma_window_cap(istore, cap, size, window):
    assert istore.lzma_window(size, cap) == window


def test_lzma_window_covers_repeats_far_apart(istore, tmp_path, rand_bytes):
    block = rand_bytes(700_000, seed=3)
    src = _write(tmp_path / "r.bin", block + rand_bytes(700_000, seed=4) + block)  # repeat is 1.4 MB away

    res = istore.encode_path(src, tmp_path / "r.png")

    assert res.meta.method == "lzma" and res.meta.stored_size < 1_500_000
    assert istore.decode_path(tmp_path / "r.png", tmp_path / "out").out.read_bytes() == src.read_bytes()


def _far_repeat(tmp_path, rand_bytes):
    block = rand_bytes(300_000, seed=11)
    return _write(tmp_path / "f.bin", block + rand_bytes(300_000, seed=12) + block)  # repeat is 300 KB away


def test_smaller_window_trades_size_for_memory_and_still_decodes(istore, tmp_path, rand_bytes):
    src = _far_repeat(tmp_path, rand_bytes)

    wide = istore.encode_path(src, tmp_path / "wide.png")
    narrow = istore.encode_path(src, tmp_path / "narrow.png", window=64 << 10)

    assert wide.meta.stored_size < 650_000 < narrow.meta.stored_size
    for name in ("wide", "narrow"):
        assert istore.decode_path(tmp_path / f"{name}.png", tmp_path / name).out.read_bytes() == src.read_bytes()


def test_lzma_payload_records_its_window(istore, tmp_path, rand_bytes):
    src = _far_repeat(tmp_path, rand_bytes)
    img, meta = istore.encode(src.open("rb"), "f.bin", True, 0, window=1 << 20)
    px = istore._Pixels(img)
    px.read(istore.HEADER.size + len(meta.name.encode()))
    assert meta.method == "lzma" and px.read(1)[0] == 20


@pytest.mark.parametrize("byte", [0, 11, 31, 255])
def test_bad_lzma_window_byte_is_rejected(istore, byte):
    with pytest.raises(ImageStoreError) as ei:
        b"".join(istore._unlzma(iter([bytes([byte]) + b"junk"]), 100))
    assert ei.value.kind == "zlib"


def test_hostile_window_byte_cannot_force_a_big_allocation(istore, monkeypatch):
    seen = []
    real = istore.lzma_filters
    monkeypatch.setattr(istore, "lzma_filters", lambda w: seen.append(w) or real(w))
    with pytest.raises(ImageStoreError):
        b"".join(istore._unlzma(iter([bytes([30]) + b"junk"]), 1000))  # claims a 1 GiB window for 1000 bytes
    assert seen == [4096]


@pytest.mark.parametrize("text,mib", [("1", 1), ("64", 64), ("1024", 1024)])
def test_window_option_accepts_mib(istore, text, mib):
    assert istore.build_parser().parse_args(["encode", "x", "--window", text]).window == mib << 20


@pytest.mark.parametrize("text", ["0", "1025", "-4", "big", "1.5"])
def test_window_option_rejects_out_of_range(istore, text, capsys):
    with pytest.raises(SystemExit):
        istore.build_parser().parse_args(["encode", "x", "--window", text])
    assert "1 to 1024" in capsys.readouterr().err


def test_window_defaults_to_64_mib(istore):
    assert istore.build_parser().parse_args(["encode", "x"]).window == 64 << 20


# ---------------------------------------------------- a Python built without lzma

def test_encode_falls_back_to_zlib_without_lzma(istore, tmp_path, monkeypatch):
    monkeypatch.setattr(istore, "lzma", None)
    src = _write(tmp_path / "t.txt", _text_blob())
    res = istore.encode_path(src, tmp_path / "t.png")
    assert res.meta.method == "zlib"
    assert istore.decode_path(tmp_path / "t.png", tmp_path / "out").out.read_bytes() == src.read_bytes()


def test_reading_an_lzma_image_without_lzma_explains_itself(istore, tmp_path, monkeypatch):
    src = _write(tmp_path / "t.txt", _text_blob())
    istore.encode_path(src, tmp_path / "t.png")
    monkeypatch.setattr(istore, "lzma", None)
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(tmp_path / "t.png", tmp_path / "out")
    assert ei.value.kind == "no_lzma"
    assert not (tmp_path / "out").exists()
    assert "xz" in istore.explain(ei.value).hint


def test_the_tool_starts_and_works_with_the_lzma_import_blocked(istore, tmp_path):
    import subprocess
    import sys
    script = (
        "import sys; sys.modules['lzma'] = None\n"
        "import imagestore as m, pathlib\n"
        "assert not m.HAVE_LZMA\n"
        f"src = pathlib.Path({str(tmp_path)!r}) / 'a.txt'; src.write_text('hello ' * 4000)\n"
        "res = m.encode_path(src, src.with_suffix('.png'))\n"
        "assert res.meta.method == 'zlib', res.meta.method\n"
        "out = m.decode_path(src.with_suffix('.png'), src.parent / 'out').out\n"
        "assert out.read_text() == 'hello ' * 4000\n"
    )
    done = subprocess.run([sys.executable, "-c", script], cwd=istore.__file__.rsplit("/", 1)[0],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
