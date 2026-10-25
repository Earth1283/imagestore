import hashlib
import io
import os
import struct
import zlib

import numpy as np
import pytest
from PIL import Image, PngImagePlugin

from exact.core import png_io
from exact.core.pixels import buffer_sha256
from exact.core.png_chunks import read_chunk_layout, write_chunk, PNG_SIGNATURE
from exact.core.png_io import (
    SaveVerificationError,
    UnsupportedPngError,
    load_png,
    plan_save,
    save_png,
    save_png_verified,
)


def pillow_pixels(path):
    with Image.open(path) as im:
        return np.asarray(im).copy(), im.mode


def raw_png(path, width, height, color_type, bit_depth, rows: bytes, extra_chunks=(), interlace=0):
    with open(path, "wb") as fp:
        fp.write(PNG_SIGNATURE)
        write_chunk(fp, b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace))
        for ctype, data in extra_chunks:
            write_chunk(fp, ctype, data)
        write_chunk(fp, b"IDAT", zlib.compress(rows))
        write_chunk(fp, b"IEND", b"")


def test_rgba_roundtrip_is_byte_identical(tmp_path, payload_rgba):
    path = str(tmp_path / "a.png")
    save_png(path, payload_rgba)
    loaded = load_png(path)
    assert loaded.mode == "RGBA"
    assert loaded.pixels.shape == payload_rgba.shape
    assert loaded.pixels.tobytes() == payload_rgba.tobytes()
    decoded, mode = pillow_pixels(path)
    assert mode == "RGBA" and decoded.tobytes() == payload_rgba.tobytes()


def test_rgb_roundtrip_stays_rgb(tmp_path, payload_rgb):
    path = str(tmp_path / "b.png")
    save_png(path, payload_rgb)
    loaded = load_png(path)
    assert loaded.mode == "RGB" and loaded.pixels.shape[2] == 3
    assert loaded.layout.ihdr.color_type == 2
    assert loaded.pixels.tobytes() == payload_rgb.tobytes()


def test_load_save_without_edits_preserves_pillow_written_file(tmp_path, payload_rgba):
    src = str(tmp_path / "pillow.png")
    Image.fromarray(payload_rgba, "RGBA").save(src)
    loaded = load_png(src)
    out = str(tmp_path / "out.png")
    save_png(out, loaded.pixels)
    assert buffer_sha256(load_png(out).pixels) == buffer_sha256(payload_rgba)


def test_transparent_pixels_keep_nonzero_rgb(tmp_path):
    arr = np.zeros((4, 4, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = 0xDE, 0xAD, 0xBE
    arr[..., 3] = 0
    arr[1, 1] = (1, 2, 3, 0)
    path = str(tmp_path / "t.png")
    save_png(path, arr)
    assert load_png(path).pixels.tobytes() == arr.tobytes()
    assert pillow_pixels(path)[0].tobytes() == arr.tobytes()


def test_arbitrary_binary_in_all_four_channels(tmp_path):
    data = hashlib.sha256(b"seed").digest() * 4096  # 131072 bytes
    arr = np.frombuffer(data, np.uint8).reshape(128, 256, 4).copy()
    arr[0] = np.arange(256, dtype=np.uint8)[:, None]  # every byte value in every channel
    path = str(tmp_path / "bin.png")
    report = save_png(path, arr)
    assert report.pixel_sha256 == hashlib.sha256(arr.tobytes()).hexdigest()
    assert load_png(path).pixels.tobytes() == arr.tobytes()


def test_alpha_channel_preserved_exactly(tmp_path):
    arr = np.zeros((16, 16, 4), np.uint8)
    arr[..., 3] = np.arange(256, dtype=np.uint8).reshape(16, 16)
    arr[..., :3] = 200
    path = str(tmp_path / "alpha.png")
    save_png(path, arr)
    out = load_png(path).pixels
    assert np.array_equal(out[..., 3], arr[..., 3])
    assert np.array_equal(out[..., :3], arr[..., :3])  # no premultiplication


def test_filters_all_exercised_and_decoded_correctly(tmp_path):
    rng = np.random.default_rng(5)
    smooth = np.cumsum(rng.integers(0, 3, (64, 64, 4)), axis=1).astype(np.uint8)
    path = str(tmp_path / "smooth.png")
    save_png(path, np.ascontiguousarray(smooth))
    assert load_png(path).pixels.tobytes() == smooth.tobytes()


@pytest.mark.parametrize("color_type,bit_depth,bpp", [(2, 16, 6), (6, 16, 8), (0, 8, 1), (4, 8, 2)])
def test_unsupported_modes_are_refused_not_converted(tmp_path, color_type, bit_depth, bpp):
    w, h = 3, 2
    rows = b"".join(b"\x00" + bytes(range(w * bpp)) for _ in range(h))
    path = str(tmp_path / "u.png")
    raw_png(path, w, h, color_type, bit_depth, rows)
    with pytest.raises(UnsupportedPngError):
        load_png(path)


def test_palette_png_refused(tmp_path):
    path = str(tmp_path / "p.png")
    Image.new("P", (4, 4)).save(path)
    with pytest.raises(UnsupportedPngError):
        load_png(path)


def test_ancillary_chunks_preserved_and_unknown_unsafe_dropped_after_edit(tmp_path, payload_rgba):
    src = str(tmp_path / "meta.png")
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "ImageStore v2")
    info.add(b"prVt", b"private-safe")  # lowercase 4th letter: safe to copy
    info.add(b"prVT", b"private-unsafe")
    Image.fromarray(payload_rgba, "RGBA").save(src, pnginfo=info, dpi=(300, 150), icc_profile=b"fake-icc")
    loaded = load_png(src)

    plan = plan_save(str(tmp_path / "same.png"), loaded.pixels, loaded.layout, pixels_changed=False, transposed=False)
    names = [c.name for c in plan.before_idat + plan.after_idat]
    assert {"tEXt", "prVt", "prVT", "pHYs", "iCCP"} <= set(names)
    assert not plan.lossy_metadata

    plan = plan_save(str(tmp_path / "edited.png"), loaded.pixels, loaded.layout, pixels_changed=True, transposed=True)
    report = save_png_verified(loaded.pixels, plan)
    out = read_chunk_layout(report.dest)
    kept = {c.type: c.data for c in out.before_idat + out.after_idat}
    assert b"prVT" not in kept and b"prVt" in kept
    assert [d.name for d in plan.dropped] == ["prVT"]
    assert kept[b"tEXt"] == b"Comment\x00ImageStore v2"
    x, y, unit = struct.unpack(">IIB", kept[b"pHYs"])
    orig_x, orig_y, _ = struct.unpack(">IIB", loaded.layout.find(b"pHYs")[0].data)
    assert (x, y) == (orig_y, orig_x)
    with Image.open(report.dest) as im:
        assert im.info.get("icc_profile") == b"fake-icc"


def test_rgb_with_trns_round_trips(tmp_path, payload_rgb):
    src = str(tmp_path / "trns.png")
    Image.fromarray(payload_rgb, "RGB").save(src, transparency=(1, 2, 3))
    loaded = load_png(src)
    assert loaded.mode == "RGB"
    assert any("tRNS" in w for w in loaded.warnings)
    plan = plan_save(str(tmp_path / "o.png"), loaded.pixels, loaded.layout, pixels_changed=False, transposed=False)
    save_png_verified(loaded.pixels, plan)
    assert read_chunk_layout(str(tmp_path / "o.png")).has(b"tRNS")
    assert load_png(str(tmp_path / "o.png")).pixels.tobytes() == payload_rgb.tobytes()


ADAM7 = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]


def test_interlaced_source_loads_exactly(tmp_path, payload_rgba):
    src = str(tmp_path / "interlaced.png")
    h, w = payload_rgba.shape[:2]
    data = b""
    for x0, y0, dx, dy in ADAM7:
        sub = payload_rgba[y0::dy, x0::dx]
        if sub.size:
            data += b"".join(b"\x00" + row.tobytes() for row in sub)
    raw_png(src, w, h, 6, 8, data, interlace=1)
    assert read_chunk_layout(src).ihdr.interlace == 1
    loaded = load_png(src)
    assert loaded.pixels.tobytes() == payload_rgba.tobytes()
    assert any("interlaced" in w for w in loaded.warnings)


def test_failed_verification_leaves_original_untouched(tmp_path, payload_rgba, monkeypatch):
    path = str(tmp_path / "keep.png")
    save_png(path, payload_rgba)
    before = open(path, "rb").read()

    real_encode = png_io.encode_png

    def corrupting_encode(fp, pixels, plan, **kw):
        bad = pixels.copy()
        bad[10, 20, 3] ^= 0x01
        real_encode(fp, bad, plan, **kw)

    monkeypatch.setattr(png_io, "encode_png", corrupting_encode)
    edited = payload_rgba.copy()
    edited[0, 0] = 0
    with pytest.raises(SaveVerificationError, match=r"\(20, 10\)"):
        save_png(path, edited)
    assert open(path, "rb").read() == before
    assert [p for p in os.listdir(tmp_path) if p.endswith(".exact-tmp")] == []


def test_failed_write_leaves_original_untouched(tmp_path, payload_rgba, monkeypatch):
    path = str(tmp_path / "keep.png")
    save_png(path, payload_rgba)
    before = open(path, "rb").read()

    def exploding_encode(fp, pixels, plan, **kw):
        fp.write(b"\x89PNG partial")
        raise OSError("disk full")

    monkeypatch.setattr(png_io, "encode_png", exploding_encode)
    with pytest.raises(OSError):
        save_png(path, payload_rgba)
    assert open(path, "rb").read() == before
    assert [p for p in os.listdir(tmp_path) if p.endswith(".exact-tmp")] == []


def test_save_preserves_file_permissions(tmp_path, payload_rgba):
    path = str(tmp_path / "perm.png")
    save_png(path, payload_rgba)
    os.chmod(path, 0o640)
    save_png(path, payload_rgba)
    assert os.stat(path).st_mode & 0o777 == 0o640


def test_sha256_is_over_canonical_bytes(payload_rgba):
    assert buffer_sha256(payload_rgba) == hashlib.sha256(payload_rgba.tobytes()).hexdigest()


def test_layout_reports_crc_errors(tmp_path, payload_rgba):
    path = str(tmp_path / "crc.png")
    save_png(path, payload_rgba)
    data = bytearray(open(path, "rb").read())
    data[29] ^= 0xFF  # last byte of the IHDR CRC
    open(path, "wb").write(bytes(data))
    assert read_chunk_layout(path).crc_errors == ["IHDR"]
