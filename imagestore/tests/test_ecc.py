"""Reed-Solomon error correction: the codec, the image layout, damage it must repair and damage it must refuse."""
from __future__ import annotations

import io
import random

import pytest
from PIL import Image

from imagestore import ImageStoreError

PAYLOAD = random.Random(5).randbytes(60_000)


def _damage(png, dst, ranges, xor=0xA5):
    """Re-save an RGBA PNG with each (start, stop) range of its pixel bytes XORed."""
    with Image.open(png) as im:
        im.load()
        raw, size = bytearray(im.tobytes()), im.size
    for start, stop in ranges:
        for i in range(start, min(stop, len(raw))):
            raw[i] ^= xor
    Image.frombytes("RGBA", size, bytes(raw)).save(dst, format="PNG")
    return dst, len(raw)


@pytest.fixture
def archive(istore, tmp_path):
    def make(percent, data=PAYLOAD, **kw):
        src = tmp_path / "blob.bin"
        src.write_bytes(data)
        png = tmp_path / f"blob{percent}.png"
        istore.encode_path(src, png, ecc=percent, compress=False, force=True, **kw)
        return png, data
    return make


def _restore(istore, png, tmp_path):
    out = tmp_path / "out"
    res = istore.decode_path(png, out, force=True)
    return res, res.out.read_bytes()


# ------------------------------------------------------------------------ codec

@pytest.mark.parametrize("parity", [8, 16, 32, 64, 128])
def test_codec_repairs_up_to_half_the_parity_and_is_exact(istore, parity):
    rng, rs, k = random.Random(parity), istore._rs(parity), 255 - parity
    for _ in range(40):
        data = rng.randbytes(k)
        block = bytearray(data) + rs.parity_of(data).to_bytes(parity, "big")
        good = bytes(block)
        for pos in rng.sample(range(255), rng.randint(0, parity // 2)):
            block[pos] ^= rng.randint(1, 255)
        assert rs.repair(block) and bytes(block) == good


@pytest.mark.parametrize("parity", [8, 32])
def test_codec_never_returns_an_invalid_block_when_overwhelmed(istore, parity):
    rng, rs, k = random.Random(1), istore._rs(parity), 255 - parity
    for _ in range(60):
        data = rng.randbytes(k)
        block = bytearray(data) + rs.parity_of(data).to_bytes(parity, "big")
        for pos in rng.sample(range(255), parity):
            block[pos] ^= rng.randint(1, 255)
        before = bytes(block)
        if rs.repair(block):
            assert rs.parity_of(block[:k]) == int.from_bytes(block[k:], "big")
        else:
            assert bytes(block) == before  # a failed repair leaves the block untouched


@pytest.mark.parametrize("percent,parity", [(0, 0), (-1, 0), (1, 8), (5, 16), (12, 32), (25, 64), (50, 128), (99, 128)])
def test_percent_to_parity(istore, percent, parity):
    assert istore.ecc_parity_bytes(percent) == parity


# ------------------------------------------------------------------- round trips

@pytest.mark.parametrize("percent", [1, 5, 12, 25, 50])
def test_clean_round_trip_at_every_level(istore, archive, tmp_path, percent):
    png, data = archive(percent)
    res, got = _restore(istore, png, tmp_path)
    assert got == data
    e = res.meta.ecc
    assert e.parity == istore.ecc_parity_bytes(percent) and e.repaired_bytes == 0 and e.failed_blocks == 0
    assert res.meta.flags & istore.FLAG_ECC


@pytest.mark.parametrize("data", [b"", b"x", b"hello world"])
def test_tiny_payloads_round_trip(istore, archive, tmp_path, data):
    png, _ = archive(25, data)
    assert _restore(istore, png, tmp_path)[1] == data


def test_ecc_costs_about_its_percentage(istore, archive, tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(PAYLOAD)
    plain = istore.encode_path(src, tmp_path / "p.png", compress=False)
    ecc = istore.encode_path(src, tmp_path / "e.png", compress=False, ecc=25)
    assert 1.2 < ecc.capacity / plain.capacity < 1.45
    assert ecc.padding < 2000  # parity is not counted as padding


def test_ecc_works_with_compression_and_folders(istore, tmp_path, regular_files):
    proj = tmp_path / "proj"
    files = {f"d{i}/f.txt": (f"line {i}\n" * 300).encode() for i in range(8)}
    for rel, blob in files.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_bytes(blob)
    png = tmp_path / "proj.png"
    res = istore.encode_path(proj, png, ecc=12)
    assert res.meta.compressed
    out = tmp_path / "out"
    istore.decode_path(png, out)
    assert regular_files(out / "proj") == files


# ------------------------------------------------------------------ damage

def test_repairs_a_stripe_of_damage_and_a_wiped_header(istore, archive, tmp_path):
    png, data = archive(25)
    with Image.open(png) as im:
        n = im.width * im.height * 4
    blocks = n // 255
    bad, _ = _damage(png, tmp_path / "bad.png", [(n // 3, n // 3 + 28 * blocks), (0, 400)])

    res, got = _restore(istore, bad, tmp_path)

    assert got == data
    assert res.meta.ecc.repaired_bytes > 0 and res.meta.ecc.failed_blocks == 0


def test_repairs_scattered_flips(istore, archive, tmp_path):
    png, data = archive(12)
    with Image.open(png) as im:
        raw, size = bytearray(im.tobytes()), im.size
    rng = random.Random(3)
    for i in rng.sample(range(len(raw)), len(raw) // 40):  # 2.5% of bytes, anywhere
        raw[i] ^= rng.randint(1, 255)
    bad = tmp_path / "scatter.png"
    Image.frombytes("RGBA", size, bytes(raw)).save(bad)
    assert _restore(istore, bad, tmp_path)[1] == data


def test_the_same_damage_without_ecc_is_fatal(istore, tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(PAYLOAD)
    png = tmp_path / "plain.png"
    istore.encode_path(src, png, compress=False)
    bad, _ = _damage(png, tmp_path / "bad.png", [(5000, 5400)])
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(bad, tmp_path / "out")
    assert ei.value.kind == "sha"


def test_damage_beyond_the_parity_is_refused_and_writes_nothing(istore, archive, tmp_path):
    png, _ = archive(12)
    with Image.open(png) as im:
        n = im.width * im.height * 4
    blocks = n // 255
    # about 20 blocks fully scrambled, the rest intact: recognised, but not repairable
    ranges = [(j * blocks, j * blocks + 20) for j in range(0, 200, 2)]
    bad, _ = _damage(png, tmp_path / "bad.png", ranges)
    out = tmp_path / "out"

    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(bad, out)

    assert ei.value.kind == "ecc" and "too damaged" in str(ei.value)
    assert not out.exists()


def test_inspect_reports_repairs_and_failures(istore, archive, tmp_path):
    png, _ = archive(25)
    with Image.open(png) as im:
        n = im.width * im.height * 4
    clean = istore.inspect_path(png)
    assert clean.ok and clean.meta.ecc.repaired_bytes == 0
    bad, _ = _damage(png, tmp_path / "bad.png", [(n // 5, n // 5 + 10 * (n // 255))])
    fixed = istore.inspect_path(bad)
    assert fixed.ok and fixed.meta.ecc.repaired_bytes > 0


def test_ecc_detection_survives_every_block_being_damaged(istore, archive, tmp_path):
    png, data = archive(50)
    with Image.open(png) as im:
        n = im.width * im.height * 4
    bad, _ = _damage(png, tmp_path / "bad.png", [(0, n // 255 * 60)])  # 60 bytes lost in every block
    assert _restore(istore, bad, tmp_path)[1] == data


def test_non_ecc_images_never_take_the_ecc_path(istore, tmp_path, monkeypatch):
    monkeypatch.setattr(istore, "_ecc_open", lambda *a: pytest.fail("ECC path used for a plain image"))
    src = tmp_path / "blob.bin"
    src.write_bytes(PAYLOAD)
    istore.encode_path(src, tmp_path / "p.png")
    assert istore.decode_path(tmp_path / "p.png", tmp_path / "out").out.read_bytes() == PAYLOAD


def test_foreign_png_is_still_not_an_imagestore_image(istore, tmp_path):
    png = tmp_path / "noise.png"
    Image.frombytes("RGBA", (60, 60), random.Random(2).randbytes(60 * 60 * 4)).save(png)
    with pytest.raises(ImageStoreError) as ei:
        istore.decode_path(png, tmp_path / "out")
    assert ei.value.kind == "magic"


def test_progress_phases_for_ecc(istore, tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(PAYLOAD)
    labels = []
    task = istore.Task(lambda label, done, total: labels.append(label) if not labels or labels[-1] != label else None)
    istore.encode_path(src, tmp_path / "e.png", compress=False, ecc=12, task=task)
    assert "Adding error correction" in labels and "Checking error correction" in labels  # encode, then verify


# ------------------------------------------------------------------------ CLI

@pytest.mark.parametrize("text,value", [("0", 0), ("12", 12), ("7.5", 7.5), ("50", 50)])
def test_ecc_option(istore, text, value):
    assert istore.build_parser().parse_args(["encode", "x", "--ecc", text]).ecc == value


@pytest.mark.parametrize("text", ["-1", "51", "lots"])
def test_ecc_option_rejects_out_of_range(istore, text, capsys):
    with pytest.raises(SystemExit):
        istore.build_parser().parse_args(["encode", "x", "--ecc", text])
    assert "0 to 50" in capsys.readouterr().err


def test_ecc_is_off_by_default(istore):
    assert istore.build_parser().parse_args(["encode", "x"]).ecc == 0
