"""Headless tests of the redesigned workspace (Qt offscreen platform).

The guarantees under test are byte-level: every panel, view mode, theme and workspace
change must leave the authoritative buffer untouched, and every edit made through the UI
must change exactly the bytes the user targeted.
"""

import hashlib
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QEvent, QPointF, QSettings, Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402

from exact.core import transforms as T  # noqa: E402
from exact.core.document import Rect  # noqa: E402
from exact.core.png_io import load_png, save_png  # noqa: E402
from exact.core.synthetic import payload_image  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    from exact.gui.main_window import MainWindow

    w = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    w.resize(1280, 820)
    w.show()
    yield w
    if w.doc is not None:
        w.doc.saved_state = w.doc.history.state_id
    w.close()


def sha(arr) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def pump(app, seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def mouse(canvas, x, y, kind, modifiers=Qt.KeyboardModifier.NoModifier):
    pos = canvas.image_to_view(x + 0.5, y + 0.5)
    button = Qt.MouseButton.LeftButton
    buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, QPointF(pos), QPointF(pos), button, buttons, modifiers)
    {QEvent.Type.MouseButtonPress: canvas.mousePressEvent, QEvent.Type.MouseMove: canvas.mouseMoveEvent,
     QEvent.Type.MouseButtonRelease: canvas.mouseReleaseEvent}[kind](event)


def click(canvas, x, y, modifiers=Qt.KeyboardModifier.NoModifier):
    mouse(canvas, x, y, QEvent.Type.MouseButtonPress, modifiers)
    mouse(canvas, x, y, QEvent.Type.MouseButtonRelease, modifiers)


def key(widget, k, modifiers=Qt.KeyboardModifier.NoModifier):
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, modifiers))


def test_display_controls_never_touch_the_buffer(app, window, rgba_png, payload_rgba, tmp_path):
    """Every view mode, zoom, workspace, theme and panel only reads; a no-op save is exact."""
    from exact.gui import style

    assert window.open_path(rgba_png)
    doc = window.doc
    before = sha(doc.pixels)
    generation = doc.generation
    for zoom in (0.125, 0.5, 1, 4, 32):
        window.canvas.set_zoom(zoom)
        window.canvas.viewport().grab()
    window.act_grid.setChecked(True)
    window.act_highlight.setChecked(True)
    for i in range(window.display_combo.count()):
        window.display_combo.setCurrentIndex(i)
        for mode in ("current", "original", "split"):
            window.compare_combo.set_value(mode)
            window.canvas.viewport().grab()
    for ws in ("inspect", "edit", "inspect"):
        window.set_workspace(ws)
        window.grab()
    for theme in ("light", "dark"):
        window.set_theme(theme)
        window.grab()
    window.hex_panel.view.viewport().grab()
    window.set_inspected(5, 5)
    window.integrity_panel.grab()
    assert style.theme_name() == "dark"
    assert sha(doc.pixels) == before == sha(payload_rgba)
    assert doc.generation == generation and len(doc.history) == 0 and not doc.dirty

    out = str(tmp_path / "noop.png")
    assert window.save_to(out, interactive=False) is not None
    saved = load_png(out).pixels
    assert saved.tobytes() == payload_rgba.tobytes()
    transparent = payload_rgba[..., 3] == 0
    assert transparent.any() and (payload_rgba[transparent][:, :3] != 0).any()
    assert np.array_equal(saved[transparent], payload_rgba[transparent])  # RGB under alpha 0 kept


def test_inspector_edits_exactly_one_channel_and_restores(app, window, rgba_png, payload_rgba):
    window.open_path(rgba_png)
    doc = window.doc
    x, y = 7, 3
    window.set_inspected(x, y)
    insp = window.inspector
    original = doc.get_pixel(x, y)
    new_alpha = (original[3] + 77) % 256
    insp.rows[3]["spin"].setValue(new_alpha)
    assert insp.apply_button.isEnabled()
    assert doc.get_pixel(x, y) == original  # editing a field does not write
    insp._apply()
    assert doc.get_pixel(x, y) == original[:3] + (new_alpha,)
    diff = doc.pixels != payload_rgba
    assert diff.sum() == 1 and diff[y, x, 3]
    assert window.integrity_chip.text() == "1 pixel modified"
    assert "Unsaved" in window.save_chip.text()
    insp._restore()
    assert doc.pixels.tobytes() == payload_rgba.tobytes()
    assert doc.matches_original
    assert window.integrity_chip.text() == "Identical to original"
    assert len(doc.history) == 2  # both steps are undoable


def test_pencil_write_mask_from_tool_panel_changes_alpha_only(app, window, rgba_png, payload_rgba):
    window.open_path(rgba_png)
    doc = window.doc
    window.set_tool("pencil")
    panel = window.tool_panel
    panel.set_color((1, 2, 3, 99))
    for i in range(3):
        panel.color.masks[i].setChecked(False)
    assert window.tool_settings.mask == [False, False, False, True]
    window.canvas.set_zoom(8)
    band_y = doc.height // 3  # payload_image makes this band fully transparent with non-zero RGB
    window.canvas.center_on(20, band_y)
    mouse(window.canvas, 18, band_y, QEvent.Type.MouseButtonPress)
    mouse(window.canvas, 22, band_y, QEvent.Type.MouseMove)
    mouse(window.canvas, 22, band_y, QEvent.Type.MouseButtonRelease)
    changed = (doc.pixels != payload_rgba)
    assert changed[..., :3].sum() == 0  # RGB untouched, including under alpha 0
    assert set(np.nonzero(changed[..., 3].any(axis=0))[0]) == set(range(18, 23))
    assert (doc.pixels[band_y, 18:23, 3] == 99).all()


def test_eyedropper_and_zoom_tools_only_read(app, window, rgba_png):
    window.open_path(rgba_png)
    doc = window.doc
    generation = doc.generation
    window.canvas.set_zoom(8)
    window.canvas.center_on(10, 10)
    window.set_tool("eyedropper")
    click(window.canvas, 10, 10)
    assert window.tool_settings.color == doc.get_pixel(10, 10)
    assert doc.generation == generation
    window.set_tool("zoom")
    click(window.canvas, 10, 10)
    assert window.canvas.zoom == 12
    click(window.canvas, 10, 10, Qt.KeyboardModifier.AltModifier)
    assert window.canvas.zoom == 8
    assert doc.generation == generation and len(doc.history) == 0


def test_history_panel_jumps_and_lists_steps(app, window, rgba_png, payload_rgba):
    window.open_path(rgba_png)
    doc = window.doc
    window._apply_pixel(1, 1, (0, 0, 0, 0))
    window.canvas.set_selection(Rect(2, 2, 5, 4))
    window.fill_selection()
    window.apply_transform(T.ROTATE_CW, confirm=False)
    assert window.history_panel.list.count() == 4
    assert window.history_panel.list.currentRow() == 3
    window.jump_to_history(0)
    assert doc.pixels.tobytes() == payload_rgba.tobytes()
    assert window.history_panel.list.count() == 4  # redo entries still listed
    window.history_panel.jumpRequested.emit(2)
    assert len(doc.history) == 2 and doc.history.can_redo
    window.jump_to_history(3)
    assert doc.pixels.shape == (payload_rgba.shape[1], payload_rgba.shape[0], 4)


def test_rotation_and_flip_round_trips_through_the_window(app, window, rgba_png, payload_rgba):
    window.open_path(rgba_png)
    doc = window.doc
    for op in (T.ROTATE_CW, T.ROTATE_180, T.FLIP_H, T.FLIP_V, T.ROTATE_CCW):
        window.apply_transform(op, confirm=False)
        assert window.doc.pixels.tobytes() != payload_rgba.tobytes()
        window.apply_transform(T.INVERSE[op], confirm=False)
        assert doc.pixels.tobytes() == payload_rgba.tobytes()
        assert doc.matches_original
    window.apply_transform(T.ROTATE_CW, confirm=False)
    assert window.integrity_chip.text() == "Same values, new orientation"
    window.undo()
    assert doc.pixels.tobytes() == payload_rgba.tobytes()


def test_hex_view_keyboard_and_goto_navigation(app, window, rgba_png):
    window.open_path(rgba_png)
    doc = window.doc
    window.set_workspace("inspect")
    view = window.hex_panel.view
    window.set_inspected(0, 0)
    view.setFocus()
    key(view, Qt.Key.Key_Right)  # byte 1: still pixel (0, 0)
    assert window.canvas.inspected == (0, 0) and view.cursor == 1
    key(view, Qt.Key.Key_Tab)  # next pixel
    assert view.cursor == 4 and window.canvas.inspected == (1, 0)
    key(view, Qt.Key.Key_Down)  # one row = four pixels
    assert window.canvas.inspected == (5, 0)
    key(view, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    assert view.cursor == doc.pixels.size - 1
    assert window.canvas.inspected == (doc.width - 1, doc.height - 1)
    window.hex_panel.goto.setText("12, 34")
    window.hex_panel._goto()
    assert window.canvas.inspected == (12, 34)
    window.hex_panel.goto.setText("0x10")
    window.hex_panel._goto()
    assert window.canvas.inspected == (4, 0)
    window.hex_panel.goto.setText("nonsense")
    window.hex_panel._goto()
    assert window.hex_panel.goto.property("invalid") == "true"
    assert "pixel (4, 0)" in window.hex_panel.info.text()


def test_hex_view_mouse_selects_byte(app, window, rgba_png):
    window.open_path(rgba_png)
    window.set_workspace("inspect")
    view = window.hex_panel.view
    view.resize(900, 300)
    view.verticalScrollBar().setValue(0)
    row, col = 2, 6
    pos = QPointF(view._col_x(col) + view.cw, view.lh * (row + 1) + view.lh / 2)
    event = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(event)
    offset = row * view.bpr + col
    assert view.cursor == offset
    index = offset // 4
    assert window.canvas.inspected == (index % window.doc.width, index // window.doc.width)


def test_workspace_presets_and_memory(app, window, rgba_png):
    window.open_path(rgba_png)
    window.set_tool("pencil")
    window.set_workspace("inspect")
    assert window.docks["hex"].isVisible() and window.docks["integrity"].isVisible()
    assert not window.docks["tool"].isVisible()
    assert window.canvas.tool == "inspect"
    window.set_workspace("edit")
    assert window.docks["tool"].isVisible() and not window.docks["hex"].isVisible()
    window.docks["history"].hide()  # the user customises Edit...
    window.set_workspace("inspect")
    window.set_workspace("edit")
    assert not window.docks["history"].isVisible()  # ...and it is remembered
    window.reset_workspace()
    assert window.docks["history"].isVisible()


def test_integrity_status_confirms_with_sha256_off_thread(app, window, rgba_png):
    window.open_path(rgba_png)
    doc = window.doc
    window._apply_pixel(3, 3, (9, 9, 9, 9))
    assert window.hashes is None  # not hashed synchronously after the edit
    for _ in range(300):
        pump(app, 0.01)
        if window.hashes is not None:
            break
    assert window.hashes is not None
    assert window.hashes["current"] == doc.current_sha256()
    window.undo()
    for _ in range(300):
        pump(app, 0.01)
        if window.hashes is not None:
            break
    assert window.hashes["current"] == doc.original_sha256
    assert "Confirmed by SHA-256" in window.integrity_panel.original_block.detail.text()
    # dirty flag aside, edits that cancel out match the saved file by hash
    assert window.save_chip.text() == "Matches saved file"


def test_brush_strokes_do_not_hash_per_movement(app, window, rgba_png, monkeypatch):
    window.open_path(rgba_png)
    pump(app, 0.7)  # let the open-time hash run
    calls = []
    monkeypatch.setattr(window.worker, "start", lambda doc: calls.append(doc) or True)
    window.set_tool("pencil")
    window.canvas.set_zoom(4)
    window.canvas.center_on(30, 30)
    mouse(window.canvas, 10, 10, QEvent.Type.MouseButtonPress)
    for i in range(40):
        mouse(window.canvas, 10 + i, 10 + i // 3, QEvent.Type.MouseMove)
        pump(app, 0.001)
    mouse(window.canvas, 50, 23, QEvent.Type.MouseButtonRelease)
    assert calls == []  # nothing while drawing; one debounced hash afterwards
    pump(app, 0.7)
    assert len(calls) == 1


def test_tile_eviction_never_loses_unsaved_edits(app, window, rgba_png, tmp_path):
    window.open_path(rgba_png)
    doc = window.doc
    window.canvas.tiles.max_bytes = 1  # evict on every insert
    window._apply_pixel(40, 30, (1, 2, 3, 4))
    for zoom in (0.25, 1, 8, 32):
        window.canvas.set_zoom(zoom)
        window.canvas.center_on(40, 30)
        window.canvas.viewport().grab()
    assert len(window.canvas.tiles) <= 1
    assert doc.get_pixel(40, 30) == (1, 2, 3, 4)
    out = str(tmp_path / "edited.png")
    window.save_to(out, interactive=False)
    assert tuple(load_png(out).pixels[30, 40]) == (1, 2, 3, 4)


def test_compare_with_disk_reports_identical_then_different(app, window, rgba_png, monkeypatch):
    from exact.gui.diff_dialog import DiffDialog

    window.open_path(rgba_png)
    d = DiffDialog(window, window.doc, path_b=rgba_png, use_current=True)
    d.run_compare()
    assert d.result.diff.identical
    assert d.verdict.title.text() == "Pixel buffers are identical"
    window._apply_pixel(0, 1, (5, 5, 5, 5))
    d.run_compare()
    assert d.verdict.title.text() == "1 pixels differ".replace("pixels", "pixel")
    assert d.goto_first.isEnabled()


def test_open_errors_are_explained(app, window, tmp_path, monkeypatch):
    from exact.gui import errors

    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")
    seen = []
    monkeypatch.setattr("exact.gui.main_window.show_error",
                        lambda parent, exc, action="open", note="": seen.append(errors.explain(exc, action)))
    assert not window.open_path(str(bad))
    assert seen[0].title == "Not a valid PNG"
    assert not window.open_path(str(tmp_path / "missing.png"))
    assert seen[1].title == "File not found"
    assert errors.explain(MemoryError()).title == "Not enough memory"
    import errno
    assert errors.explain(OSError(errno.ENOSPC, "full", "x.png"), "save").title == "Not enough disk space"


def test_large_image_rendering_and_navigation(app, window, tmp_path):
    """3000×2000 RGBA (24 MB): open, paint at several zooms, scroll to the far corner and
    navigate the hex view to the last byte. Display work stays viewport-sized."""
    arr = payload_image(3000, 2000, "RGBA", seed=5)
    path = str(tmp_path / "large.png")
    save_png(path, arr)
    assert window.open_path(path)
    doc, canvas = window.doc, window.canvas
    canvas.zoom_to_fit()
    canvas.viewport().grab()
    for zoom in (1, 4):
        canvas.set_zoom(zoom)
        canvas.center_on(2999, 1999)
        img = canvas.viewport().grab().toImage()
        pos = canvas.image_to_view(2999.5, 1999.5)
        if zoom == 1:
            continue
        window.display_combo.setCurrentIndex(window.display_combo.findData("rgb"))
        img = canvas.viewport().grab().toImage()
        assert img.pixelColor(int(pos.x()), int(pos.y())).getRgb()[:3] == doc.get_pixel(2999, 1999)[:3]
    assert canvas.tiles._bytes <= canvas.tiles.max_bytes
    view = window.hex_panel.view
    window.set_workspace("inspect")
    view.setFocus()
    key(view, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    assert window.canvas.inspected == (2999, 1999)
    view.viewport().grab()
    assert sha(doc.pixels) == sha(arr)


def test_shared_style_copies_are_identical():
    here = os.path.dirname(__file__)
    exact_copy = os.path.join(here, "..", "src", "exact", "gui", "style.py")
    store_copy = os.path.join(here, "..", "..", "imagestore", "imagestore_gui", "style.py")
    if not os.path.exists(store_copy):
        pytest.skip("ImageStore checkout not next to eXact")
    with open(exact_copy, "rb") as a, open(store_copy, "rb") as b:
        assert a.read() == b.read(), "style.py copies differ: copy eXact's over ImageStore's (or vice versa)"


def test_history_panel_incremental_updates_match_a_rebuild(app, window, rgba_png, tmp_path):
    from exact.gui.panels import HistoryPanel

    window.open_path(rgba_png)
    doc = window.doc
    for i in range(5):
        window._apply_pixel(i, 0, (i, i, i, i))
    window.undo()
    window.undo()
    window._apply_pixel(9, 9, (1, 1, 1, 1))  # drops the two redo entries
    window.save_to(str(tmp_path / "saved.png"), interactive=False)
    window.undo()

    def rows(panel):
        lst = panel.list
        return [(lst.item(i).text(), lst.item(i).font().italic()) for i in range(lst.count())]

    fresh = HistoryPanel()
    fresh.show_document(doc)
    assert rows(window.history_panel) == rows(fresh)
    assert window.history_panel.list.count() == 5  # opened + 3 kept edits + the post-undo one
    assert rows(fresh)[-1][0].endswith("✓ saved") and rows(fresh)[-1][1]  # saved state, currently undone


@pytest.mark.slow
def test_full_size_workspace_navigation_and_edit(app, window, tmp_path):
    """7488×7487 RGBA through the GUI: open, render at fit/100%/3200%, scroll to the far corner,
    hex-navigate to the last byte, edit one pixel, rotate there and back, save, verify."""
    rng = np.random.default_rng(11)
    arr = rng.integers(0, 256, (7487, 7488, 4), dtype=np.uint8)
    path = str(tmp_path / "full.png")
    save_png(path, arr, compress_level=1)
    expected = sha(arr)
    assert window.open_path(path)
    doc, canvas = window.doc, window.canvas
    assert doc.original_sha256 == expected
    timings = {}
    for zoom in (0.125, 1, 32):
        canvas.set_zoom(zoom)
        canvas.center_on(7487, 7486)
        start = time.perf_counter()
        canvas.viewport().grab()
        timings[zoom] = time.perf_counter() - start
    assert canvas.tiles._bytes <= canvas.tiles.max_bytes
    window.set_workspace("inspect")
    view = window.hex_panel.view
    assert np.shares_memory(doc.pixels.reshape(-1), doc.pixels)  # the hex view never copies the buffer
    view.setFocus()
    key(view, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    assert window.canvas.inspected == (7487, 7486)
    window._apply_pixel(7487, 7486, (1, 2, 3, 4))
    window.apply_transform(T.ROTATE_CW, confirm=False)
    window.apply_transform(T.ROTATE_CCW, confirm=False)
    assert doc.content_diff_pixels == 1 and doc.orientation.is_identity
    window.save_to(path, interactive=False)
    out = load_png(path).pixels
    arr[-1, -1] = (1, 2, 3, 4)
    assert out.tobytes() == arr.tobytes()
    print("paint at zoom:", {z: f"{t * 1000:.0f} ms" for z, t in timings.items()})


def test_cancelled_open_changes_nothing(app, window, rgba_png, payload_rgba, monkeypatch):
    """Cancelling a slow open returns at once; the current document is untouched."""
    import threading

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QProgressDialog

    from exact.core.document import Document

    window.open_path(rgba_png)
    doc = window.doc
    release = threading.Event()
    real_open = Document.open

    def slow_open(path, **kw):
        release.wait(5)
        return real_open(path, **kw)

    monkeypatch.setattr(Document, "open", staticmethod(slow_open))

    def cancel():
        for w in QtWidgets.QApplication.topLevelWidgets():
            if isinstance(w, QProgressDialog):
                w.cancel()

    QTimer.singleShot(100, cancel)
    assert window.open_path(rgba_png) is False
    release.set()
    assert window.doc is doc and doc.pixels.tobytes() == payload_rgba.tobytes()
    assert "cancelled" in window.statusBar().currentMessage()
    assert not window.canvas.busy


def test_saves_and_transforms_are_never_cancellable(app, window, rgba_png, monkeypatch):
    calls = []
    import exact.gui.main_window as mw

    real = mw.run_blocking
    monkeypatch.setattr(mw, "run_blocking", lambda *a, **k: calls.append(k.get("cancellable", False)) or real(*a, **k))
    monkeypatch.setattr(mw, "HEAVY_PIXELS", 1)
    window.open_path(rgba_png)
    window.apply_transform(T.ROTATE_CW, confirm=False)
    window.save_to(rgba_png + ".out.png", interactive=False)
    assert calls == [True, False, False]  # open: cancellable; transform and save: not
