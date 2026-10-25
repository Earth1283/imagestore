import numpy as np
import pytest

from exact.core.diff import ComparisonMismatch, compare_buffers, compare_files
from exact.core.png_io import save_png


def test_identical_buffers(payload_rgba):
    d = compare_buffers(payload_rgba, payload_rgba.copy())
    assert d.identical and d.visually_identical
    assert d.match_percent == 100.0
    assert d.first_diff is None and d.bbox is None
    assert d.heatmap.max() == 0


def test_counts_first_diff_and_bbox(payload_rgba):
    b = payload_rgba.copy()
    b[7, 30, 1] ^= 0xFF
    b[20, 3, :] ^= 1
    d = compare_buffers(payload_rgba, b)
    assert d.changed_pixels == 2
    assert d.changed_bytes == 5
    assert d.first_diff == (30, 7)
    assert d.first_values_a == tuple(int(v) for v in payload_rgba[7, 30])
    assert d.first_values_b == tuple(int(v) for v in b[7, 30])
    assert d.bbox == (3, 7, 30, 20)
    assert d.heatmap[7, 30] == abs(int(payload_rgba[7, 30, 1]) - int(b[7, 30, 1]))
    assert np.count_nonzero(d.heatmap) == 2


def test_invisible_changes_detected():
    a = np.zeros((4, 4, 4), np.uint8)
    a[..., :3] = 77
    b = a.copy()
    b[1, 1, :3] = 0  # what a lossy editor does to transparent pixels
    d = compare_buffers(a, b)
    assert not d.identical
    assert d.visually_identical
    assert d.invisible_pixels == 1


def test_mismatched_dimensions_or_mode():
    with pytest.raises(ComparisonMismatch):
        compare_buffers(np.zeros((2, 3, 4), np.uint8), np.zeros((3, 2, 4), np.uint8))
    with pytest.raises(ComparisonMismatch):
        compare_buffers(np.zeros((2, 3, 4), np.uint8), np.zeros((2, 3, 3), np.uint8))


def test_compare_files_distinguishes_file_and_pixel_identity(tmp_path, payload_rgba):
    a, b = str(tmp_path / "a.png"), str(tmp_path / "b.png")
    save_png(a, payload_rgba, compress_level=9)
    save_png(b, payload_rgba, compress_level=1)
    result = compare_files(a, b)
    assert not result.files_identical
    assert result.diff.identical
