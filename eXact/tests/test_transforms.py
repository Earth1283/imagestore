import itertools

import numpy as np
import pytest

from exact.core import transforms as T


@pytest.fixture
def img():
    rng = np.random.default_rng(3)
    return rng.integers(0, 256, (5, 7, 4), dtype=np.uint8)


@pytest.mark.parametrize("op", T.OPS)
def test_inverse_restores_exact_bytes(img, op):
    out = T.apply_transform(img, op)
    assert out.flags.c_contiguous
    back = T.apply_transform(out, T.INVERSE[op])
    assert back.tobytes() == img.tobytes()


@pytest.mark.parametrize("op", T.OPS)
def test_transform_is_pure_permutation(img, op):
    out = T.apply_transform(img, op)
    pixels_in = sorted(map(bytes, img.reshape(-1, 4)))
    pixels_out = sorted(map(bytes, out.reshape(-1, 4)))
    assert pixels_in == pixels_out


def test_rotation_coordinates(img):
    h, w = img.shape[:2]
    cw = T.apply_transform(img, T.ROTATE_CW)
    assert cw.shape[:2] == (w, h)
    # original (x, y) lands at (h - 1 - y, x)
    for x, y in [(0, 0), (6, 0), (3, 4)]:
        assert np.array_equal(cw[x, h - 1 - y], img[y, x])
    ccw = T.apply_transform(img, T.ROTATE_CCW)
    for x, y in [(0, 0), (6, 0), (3, 4)]:
        assert np.array_equal(ccw[w - 1 - x, y], img[y, x])


def test_four_rotations_and_double_flips_are_identity(img):
    out = img
    for _ in range(4):
        out = T.apply_transform(out, T.ROTATE_CW)
    assert out.tobytes() == img.tobytes()
    for op in (T.FLIP_H, T.FLIP_V, T.ROTATE_180):
        assert T.apply_transform(T.apply_transform(img, op), op).tobytes() == img.tobytes()


def test_orientation_tracks_any_sequence(img):
    for seq in itertools.product(T.OPS, repeat=3):
        direct = img
        orientation = T.Orientation()
        for op in seq:
            direct = T.apply_transform(direct, op)
            orientation = orientation.then(op)
        assert np.array_equal(orientation.view(img), direct), seq


def test_orientation_has_eight_distinct_elements(img):
    seen = set()
    frontier = [T.Orientation()]
    while frontier:
        o = frontier.pop()
        if o in seen:
            continue
        seen.add(o)
        frontier += [o.then(op) for op in T.OPS]
    assert len(seen) == 8
    assert len({o.view(img).tobytes() + bytes(o.view(img).shape[:2]) for o in seen}) == 8
