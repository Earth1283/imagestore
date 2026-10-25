import hashlib
import time

import numpy as np
import pytest

from exact.core import transforms as T
from exact.core.document import Document
from exact.core.png_io import load_png, save_png


def _roundtrip(tmp_path, width, height):
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
    sha = hashlib.sha256(arr.tobytes()).hexdigest()
    path = str(tmp_path / "large.png")
    report = save_png(path, arr, compress_level=1)
    assert report.pixel_sha256 == sha
    del arr
    doc = Document.open(path)
    assert doc.original_sha256 == sha
    doc.set_pixel(width - 1, height - 1, (1, 2, 3, 4))
    doc.save(compress_level=1)
    out = load_png(path).pixels
    assert tuple(out[-1, -1]) == (1, 2, 3, 4)
    assert doc.content_diff_pixels <= 1


def test_large_image_save(tmp_path):
    _roundtrip(tmp_path, 2500, 2000)


@pytest.mark.slow
def test_full_size_imagestore_image(tmp_path):
    start = time.perf_counter()
    _roundtrip(tmp_path, 7488, 7487)
    print(f"7488x7487 roundtrip: {time.perf_counter() - start:.1f}s")


@pytest.mark.slow
def test_full_size_rotation_inverse(tmp_path):
    rng = np.random.default_rng(8)
    arr = rng.integers(0, 256, (7487, 7488, 4), dtype=np.uint8)
    doc = Document(arr)
    sha = doc.original_sha256
    doc.transform(T.ROTATE_CW)
    doc.transform(T.ROTATE_CCW)
    assert doc.current_sha256() == sha
