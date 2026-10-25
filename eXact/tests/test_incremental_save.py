import os
import zlib

import numpy as np
import pytest

from exact.core import png_io
from exact.core import transforms as T
from exact.core.document import Document, Rect
from exact.core.png_io import EncodedStream, adler32_combine, load_png, save_png


@pytest.fixture
def tall_png(tmp_path):
    rng = np.random.default_rng(11)
    arr = rng.integers(0, 256, (900, 1200, 4), dtype=np.uint8)  # 4800-byte rows → ~874 rows/band
    arr[300:600] = 7  # a compressible region, exercising every filter path
    path = str(tmp_path / "tall.png")
    save_png(path, arr)
    return path


def test_adler32_combine_matches_zlib():
    rng = np.random.default_rng(0)
    for _ in range(50):
        a = rng.integers(0, 256, int(rng.integers(0, 5000)), dtype=np.uint8).tobytes()
        b = rng.integers(0, 256, int(rng.integers(0, 70000)), dtype=np.uint8).tobytes()
        assert adler32_combine(zlib.adler32(a), zlib.adler32(b), len(b)) == zlib.adler32(a + b)


def test_band_rows_split_wide_images():
    assert png_io.band_rows_for(7488 * 4, 7487) == 140


def test_second_save_reuses_clean_bands_and_matches_full_encode(tmp_path):
    rng = np.random.default_rng(12)
    arr = rng.integers(0, 256, (2000, 1500, 4), dtype=np.uint8)
    path = str(tmp_path / "big.png")
    save_png(path, arr)
    doc = Document.open(path)
    doc.set_pixel(1, 1, (0, 0, 0, 0))
    first = doc.save()
    assert first.bands_reused == 0  # source file was not written by this document

    doc.set_pixel(10, 1500, (1, 2, 3, 4))
    second = doc.save()
    assert second.bands_total > 2
    assert second.bands_reused == second.bands_total - 1
    incremental = open(path, "rb").read()

    full = str(tmp_path / "full.png")
    png_io.save_png_verified(doc.pixels, doc.plan_save(full))
    assert open(full, "rb").read() == incremental
    assert load_png(path).pixels.tobytes() == doc.pixels.tobytes()


def test_edit_on_band_boundary_invalidates_following_band(tmp_path, tall_png):
    doc = Document.open(tall_png)
    doc.save(dest=str(tmp_path / "o.png"))
    band = doc._encoded.band_rows
    doc.set_pixel(0, band - 1, (9, 9, 9, 9))  # last row of band 0 is the "up" row of band 1
    report = doc.save()
    assert report.bands_total - report.bands_reused == 2
    assert load_png(report.dest).pixels.tobytes() == doc.pixels.tobytes()


def test_undo_after_save_and_transforms_force_correct_reencode(tmp_path, tall_png):
    doc = Document.open(tall_png)
    out = str(tmp_path / "o.png")
    doc.fill_rect(Rect(0, 0, 50, 50), (1, 1, 1, 1))
    doc.save(dest=out)
    doc.undo()
    doc.save()
    assert load_png(out).pixels.tobytes() == doc.pixels.tobytes()
    doc.transform(T.FLIP_H)
    report = doc.save()
    assert report.bands_reused == 0
    assert load_png(out).pixels.tobytes() == doc.pixels.tobytes()


def test_corrupt_cache_falls_back_to_full_encode(tmp_path, tall_png, monkeypatch):
    doc = Document.open(tall_png)
    out = str(tmp_path / "o.png")
    doc.save(dest=out)
    doc.set_pixel(5, 5, (0, 1, 2, 3))
    real = EncodedStream.read_band
    calls = []

    def garbage(self, fh, index):
        calls.append(index)
        data = bytearray(real(self, fh, index))
        data[len(data) // 2] ^= 0xFF
        return bytes(data)

    monkeypatch.setattr(EncodedStream, "read_band", garbage)
    report = doc.save()
    assert calls, "cache was not consulted"
    assert report.bands_reused == 0
    assert load_png(out).pixels.tobytes() == doc.pixels.tobytes()


def test_cache_ignored_when_file_changed_on_disk(tmp_path, tall_png):
    doc = Document.open(tall_png)
    out = str(tmp_path / "o.png")
    doc.save(dest=out)
    with open(out, "ab") as fh:
        fh.write(b"tamper")
    doc.set_pixel(5, 5, (0, 1, 2, 3))
    report = doc.save()
    assert report.bands_reused == 0
    assert load_png(out).pixels.tobytes() == doc.pixels.tobytes()
    assert not any(n.endswith(".exact-tmp") for n in os.listdir(tmp_path))
