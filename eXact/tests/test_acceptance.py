"""The v0.1.0 final acceptance test, through the same Document API the GUI uses."""

import hashlib

import numpy as np

from exact.core import transforms as T
from exact.core.document import Document
from exact.core.png_io import load_png, save_png
from exact.core.synthetic import payload_image


def raw_sha(path):
    return hashlib.sha256(load_png(path).pixels.tobytes()).hexdigest()


def test_final_acceptance(tmp_path):
    initial = payload_image(301, 173, "RGBA", seed=921)
    initial_sha = hashlib.sha256(initial.tobytes()).hexdigest()
    path = str(tmp_path / "payload.png")
    save_png(path, initial)

    doc = Document.open(path)
    doc.transform(T.ROTATE_CW)
    doc.save()
    rotated = load_png(path).pixels
    assert rotated.shape == (301, 173, 4)

    doc = Document.open(path)
    doc.transform(T.ROTATE_CCW)
    doc.save()

    final = load_png(path).pixels
    assert hashlib.sha256(final.tobytes()).hexdigest() == initial_sha
    assert final.tobytes() == initial.tobytes()

    doc = Document.open(path)
    doc.set_pixel(100, 50, tuple(int(v) ^ 0xFF for v in doc.get_pixel(100, 50)))
    doc.save()
    edited = load_png(path).pixels
    changed = (edited != initial).any(axis=2)
    assert changed.sum() == 1 and changed[50, 100]
    assert raw_sha(path) != initial_sha
    np.testing.assert_array_equal(edited[~changed], initial[~changed])
