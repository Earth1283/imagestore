import numpy as np
import pytest

from exact.core import transforms as T
from exact.core.document import ChannelMismatchError, Clip, Document, Rect, bresenham
from exact.core.pixels import buffer_sha256


@pytest.fixture
def doc(payload_rgba):
    return Document(payload_rgba.copy())


def changed_mask(a, b):
    return (a != b).any(axis=2)


def test_one_pixel_edit_changes_exactly_one_pixel(doc):
    before = doc.pixels.copy()
    summary = doc.set_pixel(10, 20, (1, 2, 3, 4))
    mask = changed_mask(before, doc.pixels)
    assert mask.sum() == 1 and mask[20, 10]
    assert summary.changed_pixels == 1
    assert summary.bbox == Rect(10, 20, 1, 1)
    assert doc.content_diff_pixels == 1
    untouched = ~mask
    assert np.array_equal(before[untouched], doc.pixels[untouched])


def test_pixel_edit_into_transparent_pixel_touches_nothing_else(doc):
    y = doc.height // 3  # synthetic band of alpha=0 with non-zero RGB
    assert doc.pixels[y, 5, 3] == 0 and doc.pixels[y, 5, :3].any()
    before = doc.pixels.copy()
    doc.begin_stroke((9, 9, 9, 255), size=1)
    doc.stroke_to(5, y)
    summary = doc.end_stroke()
    assert summary.changed_pixels == 1
    assert tuple(doc.pixels[y, 5]) == (9, 9, 9, 255)
    mask = changed_mask(before, doc.pixels)
    assert np.array_equal(before[~mask], doc.pixels[~mask])
    for nx in (4, 6):
        assert tuple(doc.pixels[y, nx]) == tuple(before[y, nx])


def test_setting_same_value_is_reported_as_zero_changes(doc):
    v = doc.get_pixel(3, 3)
    summary = doc.set_pixel(3, 3, v)
    assert summary.changed_pixels == 0
    assert not doc.history.can_undo


def test_pencil_stroke_touches_only_line_pixels(doc):
    before = doc.pixels.copy()
    doc.begin_stroke((0, 0, 0, 0), size=1)
    doc.stroke_to(2, 2)
    doc.stroke_to(30, 17)
    summary = doc.end_stroke()
    line = set(bresenham(2, 2, 30, 17))
    mask = changed_mask(before, doc.pixels)
    changed = {(int(x), int(y)) for y, x in zip(*np.nonzero(mask))}
    assert changed <= line
    assert summary.changed_pixels == len(changed)
    assert summary.bbox == Rect.from_points(2, 2, 30, 17)


def test_channel_mask_writes_only_selected_channels(doc):
    before = doc.pixels.copy()
    doc.begin_stroke((7, 7, 7, 7), mask=[False, False, False, True])
    doc.stroke_to(40, 40)
    doc.end_stroke()
    assert np.array_equal(doc.pixels[40, 40, :3], before[40, 40, :3])
    assert doc.pixels[40, 40, 3] == 7


def test_brush_size_and_clipping_at_edges(doc):
    before = doc.pixels.copy()
    doc.begin_stroke((1, 1, 1, 1), size=3)
    doc.stroke_to(0, 0)
    summary = doc.end_stroke()
    mask = changed_mask(before, doc.pixels)
    assert mask[:2, :2].all() and mask.sum() == summary.changed_pixels <= 4


def test_undo_redo_exactness(doc):
    snapshots = [doc.pixels.tobytes()]
    doc.set_pixel(1, 1, (0, 0, 0, 0))
    snapshots.append(doc.pixels.tobytes())
    doc.begin_stroke((255, 255, 255, 255), size=2)
    doc.stroke_to(5, 5)
    doc.stroke_to(50, 30)
    doc.end_stroke()
    snapshots.append(doc.pixels.tobytes())
    doc.fill_rect(Rect(10, 10, 20, 15), (5, 6, 7, 8))
    snapshots.append(doc.pixels.tobytes())
    doc.transform(T.ROTATE_CW)
    snapshots.append(doc.pixels.tobytes())
    doc.paste(doc.copy_rect(Rect(0, 0, 8, 8)), 30, 40)
    snapshots.append(doc.pixels.tobytes())

    for expected in reversed(snapshots[:-1]):
        doc.undo()
        assert doc.pixels.tobytes() == expected
    assert doc.matches_original
    assert doc.content_diff_pixels == 0 and doc.content_diff_bytes == 0
    assert doc.undo() is None
    for expected in snapshots[1:]:
        doc.redo()
        assert doc.pixels.tobytes() == expected
        assert (doc.content_diff_pixels, doc.content_diff_bytes) == doc.recount_content_diff()


def test_incremental_diff_counters_match_full_rescan(doc):
    rng = np.random.default_rng(0)
    for i in range(60):
        op = rng.integers(0, 4)
        if op == 0:
            doc.set_pixel(int(rng.integers(doc.width)), int(rng.integers(doc.height)),
                          rng.integers(0, 256, 4))
        elif op == 1:
            doc.transform(T.OPS[int(rng.integers(len(T.OPS)))])
        elif op == 2:
            doc.fill_rect(Rect(int(rng.integers(doc.width)), int(rng.integers(doc.height)), 9, 5),
                          rng.integers(0, 256, 4), mask=rng.integers(0, 2, 4).astype(bool))
        else:
            doc.undo()
        assert (doc.content_diff_pixels, doc.content_diff_bytes) == doc.recount_content_diff(), i


def test_changed_pixel_and_byte_counts(doc):
    original = doc.pixels.copy()
    doc.set_pixel(0, 0, (original[0, 0, 0] ^ 1, *original[0, 0, 1:]))
    doc.set_pixel(1, 0, [(int(v) + 1) % 256 for v in original[0, 1]])
    assert doc.content_diff_pixels == 2
    assert doc.content_diff_bytes == 5
    assert doc.positional_diff() == (2, 5)


def test_rotation_keeps_content_diff_and_reports_positional(doc):
    doc.set_pixel(3, 4, (0, 0, 0, 0))
    doc.transform(T.ROTATE_CW)
    assert doc.content_diff_pixels == 1
    assert doc.dimensions_changed
    assert doc.positional_diff() is None


def test_paste_writes_only_target_rectangle(doc):
    before = doc.pixels.copy()
    clip = Clip(np.full((3, 4, 4), 0xAB, np.uint8), "RGBA")
    summary = doc.paste(clip, doc.width - 2, 5)
    mask = changed_mask(before, doc.pixels)
    assert mask.sum() == summary.changed_pixels
    assert not mask[:, : doc.width - 2].any()
    assert not mask[:5].any() and not mask[8:].any()
    assert "clipped" in summary.note


def test_paste_channel_mismatch_requires_permission_and_keeps_alpha(doc):
    clip = Clip(np.full((2, 2, 3), 0x11, np.uint8), "RGB")
    with pytest.raises(ChannelMismatchError):
        doc.paste(clip, 0, 2)
    alpha_before = doc.pixels[2:4, 0:2, 3].copy()
    doc.paste(clip, 0, 2, allow_channel_mismatch=True)
    assert (doc.pixels[2:4, 0:2, :3] == 0x11).all()
    assert np.array_equal(doc.pixels[2:4, 0:2, 3], alpha_before)


def test_rgb_document_stays_rgb(payload_rgb):
    doc = Document(payload_rgb.copy())
    doc.set_pixel(0, 0, (1, 2, 3))
    doc.transform(T.FLIP_H)
    assert doc.mode == "RGB" and doc.pixels.shape[2] == 3


def test_history_memory_budget_drops_oldest():
    doc = Document(np.zeros((100, 100, 4), np.uint8), history_bytes=60_000)
    for i in range(10):
        doc.fill_rect(Rect(0, 0, 100, 100), (i + 1, 0, 0, 255))
    assert len(doc.history) < 10
    assert doc.dirty
    while doc.undo():
        pass
    assert doc.pixels[0, 0, 0] != 0  # oldest steps were dropped


def test_save_marks_clean_and_dirty_tracking(tmp_path, rgba_png):
    doc = Document.open(rgba_png)
    assert not doc.dirty
    doc.set_pixel(0, 0, (1, 1, 1, 1))
    assert doc.dirty
    doc.save(dest=str(tmp_path / "o.png"))
    assert not doc.dirty
    doc.undo()
    assert doc.dirty
    doc.redo()
    assert not doc.dirty


def test_document_sha_matches_saved_sha(tmp_path, rgba_png):
    doc = Document.open(rgba_png)
    doc.set_pixel(5, 5, (0, 0, 0, 0))
    report = doc.save(dest=str(tmp_path / "o.png"))
    assert report.pixel_sha256 == buffer_sha256(doc.pixels) == doc.current_sha256()
