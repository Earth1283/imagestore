"""Power tools: they compute new values, but within the same contract as everything else.

Previews never write; Apply is one undoable step with exact accounting; untouched pixels
and unticked channels keep their bytes; results are deterministic.
"""

import hashlib
import os

import numpy as np
import pytest

from exact.core import power
from exact.core.document import Document, Rect
from exact.core.synthetic import payload_image


def sha(arr) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def float_gaussian(img: np.ndarray, sigma: float) -> np.ndarray:
    """Reference: true separable Gaussian in float64 with edge extension."""
    r = int(np.ceil(4 * sigma))
    k = np.exp(-np.arange(-r, r + 1) ** 2 / (2 * sigma * sigma))
    k /= k.sum()
    out = img.astype(np.float64)
    for axis in (0, 1):
        pad = [(0, 0)] * 3
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode="edge")
        acc = np.zeros_like(out)
        for i, w in enumerate(k):
            sl = [slice(None)] * 3
            sl[axis] = slice(i, i + out.shape[axis])
            acc += w * p[tuple(sl)]
        out = acc
    return out


# ------------------------------------------------------------------------- blur


@pytest.mark.parametrize("sigma", [0.6, 1.0, 1.9, 2.0, 4.5, 12.0])
def test_blur_tracks_a_true_gaussian(sigma):
    rng = np.random.default_rng(3)
    img = rng.integers(0, 256, (90, 120, 3), dtype=np.uint8)
    smooth = power.gaussian_blur(img, Rect(0, 0, 120, 90), sigma)
    ref = float_gaussian(img, sigma)
    # mean error well under one level on noise (the worst case for a box approximation)
    assert np.abs(smooth - ref).mean() < 1.0
    assert np.abs(smooth.astype(int) - img).mean() > 5  # it really blurred


def test_blur_selection_equals_same_area_of_whole_image_blur():
    img = payload_image(80, 70, "RGBA", seed=5)
    full = power.gaussian_blur(img, Rect(0, 0, 80, 70), 3.7)
    part = power.gaussian_blur(img, Rect(11, 9, 40, 33), 3.7)
    assert np.array_equal(part, full[9:42, 11:51])


def test_blur_is_banded_without_changing_the_result(monkeypatch):
    img = payload_image(64, 50, "RGBA", seed=6)
    whole = power.gaussian_blur(img, Rect(0, 0, 64, 50), 5.0)
    monkeypatch.setattr(power, "BAND_ELEMENTS", 300)  # force many tiny bands and strips
    assert np.array_equal(power.gaussian_blur(img, Rect(0, 0, 64, 50), 5.0), whole)


def test_blur_strength_channels_and_constants():
    img = payload_image(40, 30, "RGBA", seed=7)
    r = Rect(0, 0, 40, 30)
    assert np.array_equal(power.gaussian_blur(img, r, 4, strength=0.0), img)
    only_a = power.gaussian_blur(img, r, 4, channels=[3])
    assert np.array_equal(only_a[..., :3], img[..., :3])
    full = power.gaussian_blur(img, r, 4).astype(int)
    half = power.gaussian_blur(img, r, 4, strength=50.0).astype(int)
    assert np.abs(half - (img.astype(int) + full) / 2).max() <= 1
    flat = np.full((20, 30, 4), 77, np.uint8)
    assert (power.gaussian_blur(flat, Rect(0, 0, 30, 20), power.MAX_BLUR_RADIUS) == 77).all()
    white = np.full((20, 30, 4), 255, np.uint8)  # largest integer sums: still no overflow
    assert (power.gaussian_blur(white, Rect(0, 0, 30, 20), power.MAX_BLUR_RADIUS) == 255).all()


# ----------------------------------------------------------------------- shapes


def raster(spans, w=60, h=50):
    found = power.spans_to_mask(*spans, w, h)
    m = np.zeros((h, w), bool)
    if found:
        x, y, mask = found
        m[y:y + mask.shape[0], x:x + mask.shape[1]] = mask
    return m


def test_rectangle_outline_and_fill():
    m = raster(power.shape_spans("rect", 5, 4, 24, 19, stroke=1))
    assert m.sum() == 2 * 20 + 2 * 14
    m3 = raster(power.shape_spans("rect", 5, 4, 24, 19, stroke=3))
    assert m3.sum() == 20 * 16 - 14 * 10
    filled = raster(power.shape_spans("rect", 5, 4, 24, 19, filled=True))
    assert filled.sum() == 20 * 16 and filled[4:20, 5:25].all()


def test_ellipse_outline_is_thin_closed_and_inside_the_fill():
    filled = raster(power.shape_spans("ellipse", 3, 3, 52, 40, filled=True))
    ring = raster(power.shape_spans("ellipse", 3, 3, 52, 40, stroke=1))
    assert (ring <= filled).all() and ring.sum() < filled.sum()
    # every filled pixel touching the outside (4-neighbourhood) is on the outline
    pad = np.pad(filled, 1)
    edge = filled & ~(pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:])
    assert np.array_equal(ring, edge)
    thick = raster(power.shape_spans("ellipse", 3, 3, 52, 40, stroke=4))
    assert (ring <= thick).all() and (thick <= filled).all()


def test_line_matches_a_pencil_stroke(payload_rgba):
    a, b = Document(payload_rgba.copy()), Document(payload_rgba.copy())
    a.begin_stroke((1, 2, 3, 4), size=3)
    a.stroke_to(4, 50)
    a.stroke_to(80, 7)
    a.end_stroke()
    x, y, mask = power.spans_to_mask(*power.shape_spans("line", 4, 50, 80, 7, stroke=3), b.width, b.height)
    b.paint_mask(x, y, mask, (1, 2, 3, 4))
    assert np.array_equal(a.pixels, b.pixels)


def test_shapes_clip_to_the_image():
    found = power.spans_to_mask(*power.shape_spans("ellipse", -30, -20, 20, 25, stroke=2), 60, 50)
    x, y, mask = found
    assert x >= 0 and y >= 0 and x + mask.shape[1] <= 60 and y + mask.shape[0] <= 50
    assert power.spans_to_mask(*power.shape_spans("rect", 100, 100, 120, 120), 60, 50) is None


# ------------------------------------------------------------------------ bucket


def brute_flood(img, x, y, tol):
    h, w = img.shape[:2]
    seed = img[y, x].astype(int)
    match = (np.abs(img.astype(int) - seed) <= tol).all(axis=2)
    seen = np.zeros((h, w), bool)
    stack = [(x, y)]
    while stack:
        cx, cy = stack.pop()
        if 0 <= cx < w and 0 <= cy < h and match[cy, cx] and not seen[cy, cx]:
            seen[cy, cx] = True
            stack += [(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)]
    return seen


@pytest.mark.parametrize("tol", [0, 1, 40])
def test_flood_fill_matches_brute_force(tol):
    rng = np.random.default_rng(tol)
    img = (rng.integers(0, 3, (45, 55, 4)) * 20).astype(np.uint8)  # a maze of few values
    for x, y in ((0, 0), (27, 22), (54, 44)):
        found = power.flood_fill(img, x, y, tol)
        m = np.zeros(img.shape[:2], bool)
        fx, fy, mask = found
        m[fy:fy + mask.shape[0], fx:fx + mask.shape[1]] = mask
        assert np.array_equal(m, brute_flood(img, x, y, tol))
        g = power.flood_fill(img, x, y, tol, contiguous=False)
        glob = np.zeros(img.shape[:2], bool)
        glob[g[1]:g[1] + g[2].shape[0], g[0]:g[0] + g[2].shape[1]] = g[2]
        assert np.array_equal(glob, (np.abs(img.astype(int) - img[y, x].astype(int)) <= tol).all(axis=2))


def test_flood_treats_alpha_as_a_value():
    img = np.zeros((10, 10, 4), np.uint8)
    img[:, 5:, 3] = 255  # same RGB, different alpha
    _, _, mask = power.flood_fill(img, 0, 0)
    assert mask.shape == (10, 5)


# ---------------------------------------------------------------- value filters


def test_invert_twice_restores_every_byte(payload_rgba):
    r = Rect(0, 0, payload_rgba.shape[1], payload_rgba.shape[0])
    once = power.invert(payload_rgba, r, [0, 1, 2])
    assert np.array_equal(once[..., 3], payload_rgba[..., 3])
    assert np.array_equal(once[..., :3], 255 - payload_rgba[..., :3])
    assert np.array_equal(power.invert(once, r, [0, 1, 2]), payload_rgba)


def test_nuke_is_deterministic_crop_exact_and_spares_alpha(payload_rgba):
    full = power.nuke(payload_rgba, Rect(0, 0, 97, 61), 75)
    assert np.array_equal(full[..., 3], payload_rgba[..., 3])
    crop = power.nuke(payload_rgba, Rect(13, 21, 30, 17), 75)
    assert np.array_equal(crop, full[21:38, 13:43])  # previews show exactly what applies
    assert np.array_equal(power.nuke(payload_rgba, Rect(0, 0, 97, 61), 0), payload_rgba)


def test_cosmic_ray_flips_exactly_n_bits_in_chosen_channels(payload_rgba):
    r = Rect(10, 5, 40, 30)
    ys, xs, vals = power.cosmic_ray(payload_rgba, r, 500, seed=1, channels=[3])
    flipped = vals ^ payload_rgba[ys, xs]
    assert int(np.unpackbits(flipped).sum()) == 500
    assert not flipped[:, :3].any()
    assert ys.min() >= 5 and ys.max() < 35 and xs.min() >= 10 and xs.max() < 50
    again = power.cosmic_ray(payload_rgba, r, 500, seed=1, channels=[3])
    assert all(np.array_equal(a, b) for a, b in zip((ys, xs, vals), again))


# --------------------------------------------------------------------- document


def test_power_commits_are_exact_undoable_and_counted(payload_rgba):
    doc = Document(payload_rgba.copy())
    original = sha(doc.pixels)
    r = Rect(5, 5, 60, 40)
    blurred = power.gaussian_blur(doc.pixels, r, 3)
    s = doc.commit_region(r, blurred, "Gaussian blur 3 px")
    outside = np.ones(doc.pixels.shape[:2], bool)
    outside[5:45, 5:65] = False
    assert np.array_equal(doc.pixels[outside], payload_rgba[outside])
    assert (s.changed_pixels, s.changed_bytes) == (doc.content_diff_pixels, doc.content_diff_bytes)
    assert (doc.content_diff_pixels, doc.content_diff_bytes) == doc.recount_content_diff()
    x, y, mask = power.flood_fill(doc.pixels, 0, 0, 30)
    doc.paint_mask(x, y, mask, (9, 9, 9, 9), [False, True, False, False], "Paint bucket")
    ys, xs, vals = power.cosmic_ray(doc.pixels, Rect(0, 0, 97, 61), 50)
    doc.write_pixels(ys, xs, vals, "Cosmic ray")
    assert (doc.content_diff_pixels, doc.content_diff_bytes) == doc.recount_content_diff()
    for _ in range(3):
        doc.undo()
    assert sha(doc.pixels) == original and doc.matches_original
    for _ in range(3):
        doc.redo()
    assert (doc.content_diff_pixels, doc.content_diff_bytes) == doc.recount_content_diff()


def test_paint_mask_writes_only_ticked_channels_of_masked_pixels(payload_rgba):
    doc = Document(payload_rgba.copy())
    mask = np.zeros((10, 12), bool)
    mask[2:8, 3:9] = True
    doc.paint_mask(20, 30, mask, (1, 2, 3, 4), [False, False, False, True])
    changed = (doc.pixels != payload_rgba).any(axis=2)
    assert not changed[~np.pad(mask, ((30, 61 - 40), (20, 97 - 32)))].any()
    assert np.array_equal(doc.pixels[..., :3], payload_rgba[..., :3])
    assert (doc.pixels[32:38, 23:29, 3] == 4).all()


def test_dense_fills_store_a_rect_and_sparse_shapes_store_points(payload_rgba):
    from exact.core.history import RectPatch, SparsePatch

    doc = Document(payload_rgba.copy())
    doc.paint_mask(0, 0, np.ones((40, 40), bool), (0, 0, 0, 0))
    assert isinstance(doc.history.peek_undo(), RectPatch)
    x, y, mask = power.spans_to_mask(*power.shape_spans("ellipse", 0, 0, 90, 60), 97, 61)
    doc.paint_mask(x, y, mask, (1, 1, 1, 1))
    assert isinstance(doc.history.peek_undo(), SparsePatch)


# -------------------------------------------------------------------------- GUI


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(tmp_path):
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtCore import QSettings

    from exact.gui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    w.resize(1280, 820)
    w.show()
    app.processEvents()
    yield w
    if w.power_dialog is not None:
        w.power_dialog.close()
    if w.doc is not None:
        w.doc.saved_state = w.doc.history.state_id
    w.close()


def test_every_filter_previews_without_writing_then_applies_and_undoes(window, rgba_png):
    assert window.open_path(rgba_png)
    doc = window.doc
    for key in ("blur", "invert", "nuke", "cosmic"):
        base, generation = sha(doc.pixels), doc.generation
        window.open_filter(key)
        dialog = window.power_dialog
        dialog.actions.preview_button.setChecked(True)
        assert window.pending is not None and window.canvas.pending_visible()
        assert sha(doc.pixels) == base and doc.generation == generation, f"{key} preview wrote to the buffer"
        expected = window.pending
        dialog.apply()
        assert window.pending is None and not window.canvas.pending_visible()
        assert window.can_undo_power() and dialog.actions.undo_button.isEnabled()
        if expected.values is not None:  # Apply reused the preview, it did not recompute
            assert doc.history.peek_undo().after is expected.values
        assert (doc.content_diff_pixels, doc.content_diff_bytes) == doc.recount_content_diff()
        dialog.actions.undo_button.click()
        assert sha(doc.pixels) == base, f"{key} undo did not restore every byte"
        dialog.close()


def test_filter_targets_the_selection_only(window, rgba_png, payload_rgba):
    assert window.open_path(rgba_png)
    window.canvas.set_selection(Rect(10, 10, 20, 15))
    window.open_filter("invert")
    window.power_dialog.apply()
    changed = (window.doc.pixels != payload_rgba).any(axis=2)
    ys, xs = np.nonzero(changed)
    assert ys.min() >= 10 and ys.max() < 25 and xs.min() >= 10 and xs.max() < 30


def test_bucket_and_shape_preview_then_apply(window, rgba_png):
    assert window.open_path(rgba_png)
    doc = window.doc
    base = sha(doc.pixels)
    window.set_tool("bucket")
    window.canvas.bucketClicked.emit(3, 3)
    assert window.pending is not None and window.pending.source == "bucket" and sha(doc.pixels) == base
    window.tool_panel.power.preview_button.setChecked(False)  # Preview toggle hides it, still pending
    assert window.pending is not None and not window.canvas.pending_visible()
    window.tool_panel.power.apply_button.click()
    assert sha(doc.pixels) != base and window.can_undo_power()
    window.tool_panel.power.undo_button.click()
    assert sha(doc.pixels) == base

    window.choose_shape("rect")
    window.canvas.shapeDrawn.emit(5, 5, 30, 20)
    assert window.pending.source == "shape" and sha(doc.pixels) == base
    window.tool_panel.stroke_spin.setValue(3)  # re-tunes the same drag
    window._retune_pending()
    assert window.pending.count == 26 * 16 - 20 * 10
    window.set_tool("inspect")  # leaving the tool discards the preview, never writes
    assert window.pending is None and sha(doc.pixels) == base


def test_stale_preview_is_discarded_not_applied(window, rgba_png):
    assert window.open_path(rgba_png)
    window.set_tool("bucket")
    window.canvas.bucketClicked.emit(3, 3)
    stale = window.pending
    window.doc.set_pixel(50, 50, (1, 2, 3, 4))
    window.document_changed(window.doc.last_operation)
    assert window.pending is None
    assert window.apply_pending(stale) is None


def test_large_targets_preview_the_visible_area_but_apply_everywhere(window, tmp_path):
    from exact.core.png_io import save_png

    img = payload_image(1600, 1400, "RGBA", seed=8)  # 2.24 MP: above the live-preview limit
    path = str(tmp_path / "big.png")
    save_png(path, img)
    assert window.open_path(path)
    window.canvas.set_zoom(4)
    window.canvas.center_on(800, 700)
    window.open_filter("blur")
    dialog = window.power_dialog
    dialog.actions.preview_button.setChecked(True)
    pending = window.pending
    visible = window.canvas.visible_image_rect()
    assert pending.partial and pending.rect == visible and pending.rect.w * pending.rect.h < 1600 * 1400
    assert sha(window.doc.pixels) == sha(img)
    full = power.gaussian_blur(img, Rect(0, 0, 1600, 1400), dialog.params()["radius"],
                               dialog.params()["strength"], dialog.params()["channels"])
    # the visible preview is exactly that part of the full result
    r = pending.rect
    assert np.array_equal(pending.values, full[r.y:r.y1, r.x:r.x1])
    dialog.apply()
    assert np.array_equal(window.doc.pixels, full)


@pytest.mark.parametrize("how", ["close", "reject", "accept", "escape", "deleted"])
def test_reopening_a_filter_after_the_dialog_went_away(window, rgba_png, how):
    """Esc/reject delete the dialog without a closeEvent; reopening must not touch the dead object."""
    from PyQt6 import sip
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    assert window.open_path(rgba_png)
    window.open_filter("invert")
    dialog = window.power_dialog
    dialog.actions.preview_button.setChecked(True)
    assert window.pending is not None
    if how == "escape":
        dialog.activateWindow()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    elif how == "deleted":
        sip.delete(dialog)
    else:
        getattr(dialog, how)()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert window.power_dialog is None
    if how != "deleted":
        assert window.pending is None  # dismissing the dialog discards its staged preview
    window.open_filter("blur")
    assert window.power_dialog is not None and not sip.isdeleted(window.power_dialog)
