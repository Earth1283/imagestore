"""Headless GUI smoke test (Qt offscreen platform): the window drives the real Document."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QPointF, QSettings, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402

from exact.core import transforms as T  # noqa: E402
from exact.core.document import Rect  # noqa: E402
from exact.core.png_io import load_png  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    from exact.gui.main_window import MainWindow

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(settings)
    w.resize(1000, 700)
    w.show()
    yield w
    if w.doc is not None:
        w.doc.saved_state = w.doc.history.state_id
    w.close()


def click(canvas, x, y, kind, button=Qt.MouseButton.LeftButton):
    pos = canvas.image_to_view(x + 0.5, y + 0.5)
    buttons = button if kind != QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, QPointF(pos), QPointF(pos), button, buttons, Qt.KeyboardModifier.NoModifier)
    {QMouseEvent.Type.MouseButtonPress: canvas.mousePressEvent,
     QMouseEvent.Type.MouseMove: canvas.mouseMoveEvent,
     QMouseEvent.Type.MouseButtonRelease: canvas.mouseReleaseEvent}[kind](event)


def test_open_edit_rotate_save_cycle(window, rgba_png, payload_rgba):
    assert window.open_path(rgba_png)
    doc = window.doc
    assert doc.pixels.tobytes() == payload_rgba.tobytes()
    window.canvas.grab()

    for zoom in (0.25, 1, 8, 32):
        window.canvas.set_zoom(zoom)
        window.act_grid.setChecked(True)
        window.canvas.grab()
    for idx in range(window.display_combo.count()):
        window.display_combo.setCurrentIndex(idx)
        window.canvas.grab()
    for idx in range(window.compare_combo.count()):
        window.compare_combo.setCurrentIndex(idx)
        window.canvas.grab()
    window.act_highlight.trigger()
    window.canvas.grab()

    window.canvas.set_zoom(8)
    window.canvas.center_on(20, 20)
    window.act_tools["pencil"].trigger()
    window.tool_options.set_color((1, 2, 3, 4))
    click(window.canvas, 20, 20, QMouseEvent.Type.MouseButtonPress)
    click(window.canvas, 20, 20, QMouseEvent.Type.MouseButtonRelease)
    assert doc.get_pixel(20, 20) == (1, 2, 3, 4)
    assert doc.last_operation.changed_pixels == 1
    changed = (doc.pixels != payload_rgba).any(axis=2)
    assert changed.sum() == 1

    window.apply_transform(T.ROTATE_CW, confirm=False)
    window.canvas.grab()
    window.undo()
    window.redo()
    window.apply_transform(T.ROTATE_CCW, confirm=False)
    assert window.save_to(rgba_png, interactive=False) is not None
    saved = load_png(rgba_png).pixels
    assert ((saved != payload_rgba).any(axis=2)).sum() == 1


def test_select_copy_paste_and_inspector(window, rgba_png):
    window.open_path(rgba_png)
    doc = window.doc
    window.canvas.set_selection(Rect(0, 0, 4, 3))
    window.copy()
    window._paste_commit(10, 10)
    assert np.array_equal(doc.pixels[10:13, 10:14], doc.pixels[0:3, 0:4])
    window.set_inspected(5, 6)
    window._apply_pixel(5, 6, (9, 9, 9, 9))
    assert doc.get_pixel(5, 6) == (9, 9, 9, 9)
    assert window.inspector.hex_label.text() == "09 09 09 09"
    window.hex_panel._goto  # panel exists
    window._goto_offset(4 * (doc.width * 2 + 3))
    assert window.canvas.inspected == (3, 2)


def test_refuses_unsupported_png_without_converting(window, tmp_path, monkeypatch):
    from PIL import Image

    path = str(tmp_path / "gray.png")
    Image.new("L", (4, 4)).save(path)
    shown = []
    monkeypatch.setattr("exact.gui.main_window.show_error",
                        lambda parent, exc, action="open", note="": shown.append((exc, note)))
    assert not window.open_path(path)
    assert window.doc is None
    assert shown and "not opened" in shown[0][1]
    from exact.core.png_io import UnsupportedPngError
    from exact.gui.errors import explain

    assert isinstance(shown[0][0], UnsupportedPngError)
    assert explain(shown[0][0]).title == "Unsupported PNG format"


def test_tile_cache_invalidated_by_edits_undo_and_transforms(window, rgba_png):
    window.open_path(rgba_png)
    doc, canvas = window.doc, window.canvas
    window.display_combo.setCurrentIndex(window.display_combo.findData("rgb"))
    canvas.set_zoom(8)
    canvas.center_on(30, 30)

    def shown(x, y):
        pos = canvas.image_to_view(x + 0.5, y + 0.5)
        return canvas.viewport().grab().toImage().pixelColor(int(pos.x()), int(pos.y())).getRgb()[:3]

    assert shown(30, 30) == doc.get_pixel(30, 30)[:3]
    cached = len(canvas.tiles)
    assert cached > 0
    window._apply_pixel(30, 30, (12, 34, 56, 0))
    assert shown(30, 30) == (12, 34, 56)
    window.undo()
    assert shown(30, 30) == doc.get_pixel(30, 30)[:3]
    window.apply_transform(T.FLIP_H, confirm=False)
    assert shown(30, 30) == doc.get_pixel(30, 30)[:3]
