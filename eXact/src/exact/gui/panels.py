"""Side and bottom panels: tool properties, pixel inspector, integrity, history, image info.

Panels only read the document and emit requests; every pixel change goes through the
main window, which calls the Document (the single writer of the authoritative buffer).
Nothing here performs whole-image work on the GUI thread.
"""

from __future__ import annotations

import html
import os
import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.document import Document, Rect
from ..core.history import RectPatch, SparsePatch, TransformCommand
from ..core.pixels import pixel_offset
from . import style

PLACEHOLDER = "—"
CHANNEL_TOKENS = {"R": "ch_r", "G": "ch_g", "B": "ch_b", "A": "ch_a"}


def mono_font() -> QFont:
    return style.mono_font()


def parse_hex_color(text: str, channels: int) -> tuple[int, ...] | None:
    text = text.strip().lstrip("#")
    if len(text) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in text):
        return None
    values = [int(text[i:i + 2], 16) for i in range(0, len(text), 2)]
    if len(values) == 3:
        values.append(255)
    return tuple(values[:channels]) if channels == 3 else tuple(values)


def format_hex(values) -> str:
    return "".join(f"{v:02X}" for v in values)


def short_path(path: str | None) -> str:
    return os.path.basename(path) if path else "Untitled"


def section(text: str) -> QLabel:
    w = QLabel(text)
    style.set_role(w, "section")
    return w


def muted(text: str = "", role: str = "muted", wrap: bool = True) -> QLabel:
    w = QLabel(text)
    style.set_role(w, role)
    w.setWordWrap(wrap)
    return w


_chips: list[tuple[QLabel, str]] = []


def chip(name: str) -> QLabel:
    """A channel name in its channel colour (a label for raw values, not a colour claim)."""
    w = QLabel(name)
    w.setFixedWidth(16)
    w.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w.setStyleSheet(f"color: {style.tokens()[CHANNEL_TOKENS[name]]}; font-weight: 700;")
    _chips.append((w, name))
    return w


def restyle_chips() -> None:
    """Re-colour channel chips after a theme change."""
    alive = []
    for w, name in _chips:
        try:
            w.setStyleSheet(f"color: {style.tokens()[CHANNEL_TOKENS[name]]}; font-weight: 700;")
            alive.append((w, name))
        except RuntimeError:  # widget already deleted
            pass
    _chips[:] = alive


def swatch(values, size: int = 22, widget=None) -> object:
    alpha = values[3] if len(values) > 3 else None
    scale = widget.devicePixelRatioF() if widget is not None else 1.0
    return style.swatch_pixmap(tuple(values[:3]), alpha, size, scale)


def hline() -> QFrame:
    line = QFrame()
    line.setProperty("role", "divider")
    return line


# ------------------------------------------------------------------- tool properties


@dataclass
class ToolSettings:
    color: tuple[int, int, int, int] = (255, 0, 0, 255)
    size: int = 1
    mask: list[bool] = field(default_factory=lambda: [True, True, True, True])


TOOL_INFO = {
    "inspect": ("Inspect", "inspect", "Click a pixel to inspect it; drag to scrub. Values are shown exactly as "
                "stored. Use the Pixel panel to edit individual channels."),
    "pencil": ("Pencil", "pencil", "Writes exact channel values in square dabs: no blending, no anti-aliasing. "
               "Unticked channels are never modified. Alt+click picks a colour from the image."),
    "eyedropper": ("Eyedropper", "eyedropper", "Click a pixel to copy its raw values (alpha included) into the "
                   "pencil colour. Reads only; nothing is modified."),
    "select": ("Rectangle Select", "select", "Drag to select. Copy, paste and fill move raw channel values; "
               "nothing is blended or converted."),
    "hand": ("Hand", "hand", "Drag to pan. Space+drag or middle-drag pans with any tool."),
    "zoom": ("Zoom", "zoom", "Click to zoom in, Alt+click to zoom out. Ctrl+scroll zooms anywhere; "
             "Ctrl+0 is 100%, Ctrl+9 fits the window."),
}


class ColorEditor(QWidget):
    """Exact RGBA values for the pencil, each with its own write-mask toggle."""

    changed = pyqtSignal()

    def __init__(self, settings: ToolSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self.swatch = QToolButton()
        self.swatch.setToolTip("Choose a colour (the dialog's values are used exactly)")
        self.swatch.setAccessibleName("Pencil colour")
        self.swatch.setIconSize(QSize(44, 22))
        self.swatch.clicked.connect(self._pick)
        self.hex_edit = QLineEdit()
        self.hex_edit.setFont(style.mono_font())
        self.hex_edit.setMaxLength(9)
        self.hex_edit.setToolTip("Exact colour as RRGGBBAA (or RRGGBB, alpha FF)")
        self.hex_edit.setAccessibleName("Pencil colour hex")
        self.hex_edit.editingFinished.connect(self._hex_edited)
        top = QHBoxLayout()
        top.addWidget(self.swatch)
        top.addWidget(self.hex_edit, 1)
        grid.addLayout(top, 0, 0, 1, 4)
        head = muted("Write", "faint", wrap=False)
        head.setToolTip("Channels to write. Unticked channels keep their exact values.")
        grid.addWidget(head, 1, 0)
        self.masks: list[QCheckBox] = []
        self.sliders: list[QSlider] = []
        self.spins: list[QSpinBox] = []
        for i, name in enumerate("RGBA"):
            box = QCheckBox()
            box.setChecked(settings.mask[i])
            box.setToolTip(f"Write the {name} channel (unticked: left exactly as it is)")
            box.setAccessibleName(f"Write {name}")
            box.toggled.connect(self._mask_changed)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 255)
            slider.setAccessibleName(f"{name} value")
            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setFont(style.mono_font())
            spin.setAccessibleName(f"{name} value")
            slider.valueChanged.connect(lambda v, s=spin: s.setValue(v))
            spin.valueChanged.connect(lambda v, s=slider: s.setValue(v))
            spin.valueChanged.connect(self._values_changed)
            row = i + 2
            grid.addWidget(box, row, 0, Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(chip(name), row, 1)
            grid.addWidget(slider, row, 2)
            grid.addWidget(spin, row, 3)
            self.masks.append(box)
            self.sliders.append(slider)
            self.spins.append(spin)
        grid.setColumnStretch(2, 1)
        self._refresh()

    def set_mode(self, mode: str | None) -> None:
        rgba = mode != "RGB"
        for w in (self.masks[3], self.sliders[3], self.spins[3]):
            w.setEnabled(rgba)
        self.masks[3].setToolTip("RGB image: there is no alpha channel to write" if not rgba
                                 else "Write the A channel (unticked: left exactly as it is)")

    def set_color(self, values) -> None:
        values = tuple(int(v) for v in values) + (255,) * (4 - len(values))
        self.settings.color = values[:4]
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        for spin, v in zip(self.spins, self.settings.color):
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)
        for slider, v in zip(self.sliders, self.settings.color):
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)
        self.hex_edit.setText(format_hex(self.settings.color))
        self.swatch.setIcon(style.QIcon(swatch(self.settings.color, 22, self)))

    def _values_changed(self) -> None:
        self.settings.color = tuple(s.value() for s in self.spins)
        self.hex_edit.setText(format_hex(self.settings.color))
        self.swatch.setIcon(style.QIcon(swatch(self.settings.color, 22, self)))
        self.changed.emit()

    def _pick(self) -> None:
        r, g, b, a = self.settings.color
        color = QColorDialog.getColor(style.QColor(r, g, b, a), self, "Pencil colour",
                                      QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            self.set_color(color.getRgb())

    def _hex_edited(self) -> None:
        values = parse_hex_color(self.hex_edit.text(), 4)
        if values is None:
            self._refresh()
        else:
            self.set_color(values)

    def _mask_changed(self) -> None:
        self.settings.mask = [b.isChecked() for b in self.masks]
        self.changed.emit()


class ToolPanel(QWidget):
    """Context-sensitive properties of the active tool."""

    changed = pyqtSignal()
    selectionEdited = pyqtSignal(object)  # Rect
    actionRequested = pyqtSignal(str)  # select_all | deselect | copy | paste | paste_at | fill

    def __init__(self, settings: ToolSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        self.icon = QLabel()
        self.title = QLabel()
        style.set_role(self.title, "title")
        head.addWidget(self.icon)
        head.addWidget(self.title, 1)
        layout.addLayout(head)
        self.hint = muted()
        layout.addWidget(self.hint)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        self.pages: dict[str, QWidget] = {}

        # pencil
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 4, 0, 0)
        pl.setSpacing(8)
        pl.addWidget(section("Colour"))
        self.color = ColorEditor(settings)
        self.color.changed.connect(self.changed)
        pl.addWidget(self.color)
        pl.addWidget(section("Size"))
        size_row = QHBoxLayout()
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(1, 64)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 64)
        self.size_spin.setSuffix(" px")
        self.size_spin.setAccessibleName("Pencil size")
        self.size_slider.valueChanged.connect(self.size_spin.setValue)
        self.size_spin.valueChanged.connect(self.size_slider.setValue)
        self.size_spin.valueChanged.connect(self._size_changed)
        size_row.addWidget(self.size_slider, 1)
        size_row.addWidget(self.size_spin)
        pl.addLayout(size_row)
        pl.addWidget(muted("Square dab, centred on the cursor. Pixels outside it are never touched.", "faint"))
        self.pages["pencil"] = page

        # selection
        page = QWidget()
        sl = QVBoxLayout(page)
        sl.setContentsMargins(0, 4, 0, 0)
        sl.setSpacing(8)
        sl.addWidget(section("Selection"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        self.sel_spins: dict[str, QSpinBox] = {}
        for i, key in enumerate(("x", "y", "w", "h")):
            spin = QSpinBox()
            spin.setRange(0 if key in "xy" else 1, 1 << 20)
            spin.setFont(style.mono_font())
            spin.setAccessibleName({"x": "Selection left", "y": "Selection top", "w": "Selection width",
                                    "h": "Selection height"}[key])
            spin.editingFinished.connect(self._selection_edited)
            grid.addWidget(muted(key.upper(), "key", False), i // 2, (i % 2) * 2)
            grid.addWidget(spin, i // 2, (i % 2) * 2 + 1)
            self.sel_spins[key] = spin
        sl.addLayout(grid)
        self.sel_note = muted("No selection. Drag on the canvas, or Select All (Ctrl+A).", "faint")
        sl.addWidget(self.sel_note)
        buttons = QGridLayout()
        self.sel_buttons: dict[str, QPushButton] = {}
        for i, (key, text, icon) in enumerate((("copy", "Copy", "copy"), ("paste", "Paste…", "paste"),
                                               ("fill", "Fill with colour", "fill"), ("paste_at", "Paste at…", "goto"),
                                               ("select_all", "Select all", "select"), ("deselect", "Deselect", "close"))):
            b = QPushButton(style.icon(icon), text)
            b.clicked.connect(lambda _=False, k=key: self.actionRequested.emit(k))
            buttons.addWidget(b, i // 2, i % 2)
            self.sel_buttons[key] = b
        sl.addLayout(buttons)
        self.pages["select"] = page

        for key in ("inspect", "eyedropper", "hand", "zoom"):
            self.pages[key] = QWidget()
        for page in self.pages.values():
            self.stack.addWidget(page)
        layout.addStretch(1)
        self.set_tool("inspect")

    def set_tool(self, tool: str) -> None:
        title, icon, hint = TOOL_INFO[tool]
        self.title.setText(title)
        self.icon.setPixmap(style.icon(icon).pixmap(QSize(20, 20)))
        self.hint.setText(hint)
        self.stack.setCurrentWidget(self.pages[tool])
        page = self.pages[tool]
        self.stack.setVisible(page.layout() is not None)

    def set_mode(self, mode: str | None) -> None:
        self.color.set_mode(mode)

    def set_color(self, values) -> None:
        self.color.set_color(values)

    def _size_changed(self, value: int) -> None:
        self.settings.size = value
        self.changed.emit()

    def set_selection(self, rect: Rect | None, width: int = 0, height: int = 0, *, has_clip: bool = False) -> None:
        for key, spin in self.sel_spins.items():
            spin.blockSignals(True)
            spin.setEnabled(width > 0)
            if key in "xw":
                spin.setMaximum(max(1, width - (0 if key == "w" else 1)))
            else:
                spin.setMaximum(max(1, height - (0 if key == "h" else 1)))
            spin.setValue(getattr(rect, key) if rect else (0 if key in "xy" else 1))
            spin.blockSignals(False)
        self.sel_note.setText(f"{rect.w * rect.h:,} pixels selected" if rect
                              else "No selection. Drag on the canvas, or Select All (Ctrl+A).")
        for key in ("copy", "fill", "deselect"):
            self.sel_buttons[key].setEnabled(rect is not None)
        for key in ("paste", "paste_at"):
            self.sel_buttons[key].setEnabled(has_clip)
        self.sel_buttons["select_all"].setEnabled(width > 0)

    def _selection_edited(self) -> None:
        s = self.sel_spins
        self.selectionEdited.emit(Rect(s["x"].value(), s["y"].value(), s["w"].value(), s["h"].value()))


# ------------------------------------------------------------------ pixel inspector


class InspectorPanel(QWidget):
    """The inspected pixel: position, raw bytes, each channel in dec/hex/binary next to
    the original value, and exact per-channel editing."""

    applyRequested = pyqtSignal(int, int, object)
    useAsColor = pyqtSignal(object)
    gotoRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Document | None = None
        self.pixel: tuple[int, int] | None = None
        self.values: tuple[int, ...] = ()
        self.original: tuple[int, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        self.swatch = QLabel()
        self.swatch.setToolTip("Left: raw RGB (alpha ignored). Right: composited over a checkerboard. Display only.")
        head.addWidget(self.swatch)
        coords = QVBoxLayout()
        coords.setSpacing(0)
        self.pos_label = QLabel(PLACEHOLDER)
        self.pos_label.setFont(style.mono_font(1.15))
        self.pos_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.offset_label = muted(PLACEHOLDER, "faint", False)
        self.offset_label.setFont(style.mono_font(0.92))
        self.offset_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        coords.addWidget(self.pos_label)
        coords.addWidget(self.offset_label)
        head.addLayout(coords, 1)
        goto = QToolButton()
        goto.setIcon(style.icon("goto"))
        goto.setToolTip("Go to pixel… (Ctrl+G)")
        goto.setAccessibleName("Go to pixel")
        goto.clicked.connect(self.gotoRequested)
        head.addWidget(goto)
        layout.addLayout(head)

        raw = QHBoxLayout()
        raw.addWidget(muted("Bytes", "key", False))
        self.hex_label = QLabel(PLACEHOLDER)
        self.hex_label.setFont(style.mono_font())
        self.hex_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        raw.addWidget(self.hex_label, 1)
        layout.addLayout(raw)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        for col, text in enumerate(("", "Value", "Hex", "Binary", "Original")):
            h = muted(text, "faint", False)
            grid.addWidget(h, 0, col)
        self.rows: list[dict] = []
        for i, name in enumerate("RGBA"):
            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setFont(style.mono_font())
            spin.setAccessibleName(f"{name} channel value")
            spin.setKeyboardTracking(True)
            spin.valueChanged.connect(self._fields_changed)
            spin.lineEdit().returnPressed.connect(self._apply)
            labels = {}
            for key in ("hex", "bin", "orig"):
                lab = QLabel(PLACEHOLDER)
                lab.setFont(style.mono_font())
                lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                labels[key] = lab
            c = chip(name)
            for col, w in enumerate((c, spin, labels["hex"], labels["bin"], labels["orig"])):
                grid.addWidget(w, i + 1, col)
            self.rows.append({"chip": c, "spin": spin, **labels})
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)

        self.state = QLabel()
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

        row = QHBoxLayout()
        self.hex_edit = QLineEdit()
        self.hex_edit.setFont(style.mono_font())
        self.hex_edit.setPlaceholderText("RRGGBBAA")
        self.hex_edit.setMaxLength(9)
        self.hex_edit.setAccessibleName("Pixel value hex")
        self.hex_edit.editingFinished.connect(self._hex_to_spins)
        self.hex_edit.returnPressed.connect(self._apply)
        self.apply_button = QPushButton(style.icon("check"), "Apply")
        self.apply_button.setProperty("variant", "primary")
        self.apply_button.setToolTip("Write exactly these values to the inspected pixel (undoable). Enter in a field "
                                     "also applies.")
        self.apply_button.clicked.connect(self._apply)
        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip("Discard the edited fields and show the pixel's current values")
        self.reset_button.clicked.connect(lambda: self._load_fields(self.values))
        row.addWidget(self.hex_edit, 1)
        row.addWidget(self.reset_button)
        row.addWidget(self.apply_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.restore_button = QPushButton(style.icon("undo"), "Restore original")
        self.restore_button.setToolTip("Write this pixel's original values back (as loaded, in the current "
                                       "orientation). Undoable.")
        self.restore_button.clicked.connect(self._restore)
        self.color_button = QPushButton(style.icon("eyedropper"), "Use as colour")
        self.color_button.setToolTip("Copy these values into the pencil colour")
        self.color_button.clicked.connect(lambda: self.pixel and self.useAsColor.emit(self.values))
        row.addWidget(self.restore_button)
        row.addWidget(self.color_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        self.show_pixel(None, None)

    def show_pixel(self, doc: Document | None, pixel: tuple[int, int] | None) -> None:
        self.doc, self.pixel = doc, pixel
        enabled = doc is not None and pixel is not None
        for w in (self.hex_edit, self.apply_button, self.reset_button, self.restore_button, self.color_button):
            w.setEnabled(enabled)
        for row in self.rows:
            row["spin"].setEnabled(enabled)
        if not enabled:
            self.values = self.original = ()
            self.pos_label.setText(PLACEHOLDER)
            self.offset_label.setText("No pixel inspected")
            self.hex_label.setText(PLACEHOLDER)
            for row in self.rows:
                for key in ("hex", "bin", "orig"):
                    row[key].setText(PLACEHOLDER)
            self.swatch.clear()
            self.state.clear()
            return
        x, y = pixel
        self.values = doc.get_pixel(x, y)
        self.original = tuple(int(v) for v in doc.reference()[y, x])
        offset = pixel_offset(x, y, doc.width, doc.channels)
        self.pos_label.setText(f"({x}, {y})")
        self.offset_label.setText(f"byte {offset:,} · 0x{offset:X}")
        self.hex_label.setText(" ".join(f"{v:02X}" for v in self.values))
        self.swatch.setPixmap(swatch(self.values, 26, self))
        for i, row in enumerate(self.rows):
            visible = i < doc.channels
            for w in row.values():
                w.setVisible(visible)
            if not visible:
                continue
            v, o = self.values[i], self.original[i]
            row["hex"].setText(f"{v:02X}")
            row["bin"].setText(f"{v:04b}"[:0] + f"{v >> 4:04b} {v & 15:04b}")
            row["orig"].setText(str(o) if o != v else "=")
            row["orig"].setToolTip(f"Original value {o} (0x{o:02X})")
            row["orig"].setStyleSheet(f"color: {style.tokens()['warning' if o != v else 'text_faint']};")
        self._load_fields(self.values)

    def _load_fields(self, values) -> None:
        for row, v in zip(self.rows, values):
            row["spin"].blockSignals(True)
            row["spin"].setValue(v)
            row["spin"].blockSignals(False)
        self.hex_edit.setText(format_hex(values))
        self._fields_changed()

    def field_values(self) -> tuple[int, ...]:
        n = self.doc.channels if self.doc else 4
        return tuple(row["spin"].value() for row in self.rows[:n])

    def _fields_changed(self) -> None:
        if not self.values:
            return
        fields = self.field_values()
        edited = fields != self.values
        for row, v, cur in zip(self.rows, fields, self.values):
            row["spin"].setStyleSheet(f"border-color: {style.tokens()['accent']};" if v != cur else "")
        if self.sender() is not self.hex_edit:
            self.hex_edit.setText(format_hex(fields))
        self.apply_button.setEnabled(edited)
        self.reset_button.setEnabled(edited)
        self.restore_button.setEnabled(self.values != self.original)
        names = self.doc.channel_names if self.doc else ()
        if edited:
            changes = ", ".join(f"{n} {c}→{v}" for n, c, v in zip(names, self.values, fields) if c != v)
            style.set_pill(self.state, "info", f"Edited, not applied: {changes}")
        elif self.values == self.original:
            style.set_pill(self.state, "ok", "Matches the original")
        else:
            changes = ", ".join(f"{n} {o}→{v}" for n, o, v in zip(names, self.original, self.values) if o != v)
            style.set_pill(self.state, "warn", f"Modified: {changes}")

    def _hex_to_spins(self) -> None:
        if self.doc is None:
            return
        values = parse_hex_color(self.hex_edit.text(), self.doc.channels)
        if values is not None:
            for row, v in zip(self.rows, values):
                row["spin"].setValue(v)

    def _apply(self) -> None:
        if self.doc is None or self.pixel is None:
            return
        values = self.field_values()
        if values != self.values:
            self.applyRequested.emit(self.pixel[0], self.pixel[1], values)

    def _restore(self) -> None:
        if self.doc is not None and self.pixel is not None and self.values != self.original:
            self.applyRequested.emit(self.pixel[0], self.pixel[1], self.original)


# ------------------------------------------------------------------------ integrity


def integrity_state(doc: Document, hashes: dict | None) -> dict:
    """Exact status from the incremental counters (O(1)); SHA-256 confirms it asynchronously.

    Returns kinds and texts for "original" (vs the buffer as loaded) and "saved" (vs the
    last saved or opened file).
    """
    cur = hashes.get("current") if hashes else None
    total = doc.width * doc.height
    if doc.matches_original:
        confirmed = " Confirmed by SHA-256." if cur == doc.original_sha256 else (
            " SHA-256 check pending…" if cur is None else "")
        original = ("ok", "Identical to original",
                    "Every pixel byte equals the buffer as loaded (tracked exactly)." + confirmed)
    elif doc.content_diff_pixels == 0:
        original = ("warn", "Same values, new orientation",
                    f"{doc.orientation.describe()}: no value changed, but the byte order differs. The inverse "
                    "transform restores the original exactly.")
    else:
        pct = 100.0 * doc.content_diff_pixels / total
        original = ("warn", f"{doc.content_diff_pixels:,} pixel{'s' if doc.content_diff_pixels != 1 else ''} modified",
                    f"{doc.content_diff_bytes:,} channel bytes differ from the original ({pct:.4g}% of pixels).")
    if doc.path is None:
        saved = ("neutral", "Not saved yet", "This buffer has never been written to a file.")
    elif not doc.dirty:
        saved = ("ok", "Matches saved file", f"No edits since {short_path(doc.path)} was opened or saved.")
    elif cur is not None and cur == doc.saved_pixel_sha256:
        saved = ("ok", "Matches saved file", "There are undoable steps since the save, but they cancel out: the "
                 "SHA-256 equals the saved buffer.")
    else:
        saved = ("warn", "Unsaved changes", "The buffer differs from the last saved or opened file."
                 + (" Checking SHA-256…" if cur is None else ""))
    return {"original": original, "saved": saved}


class StatusBlock(QFrame):
    """Icon + headline + explanation, coloured by state."""

    ICONS = {"ok": ("shield", "success"), "warn": ("shield-alert", "warning"), "neutral": ("info", "text_muted"),
             "error": ("x-circle", "danger"), "info": ("info", "accent")}

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)
        self.glyph = QLabel()
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.glyph)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(section(caption))
        self.title = QLabel()
        style.set_role(self.title, "title")
        self.title.setWordWrap(True)
        self.detail = muted()
        text.addWidget(self.title)
        text.addWidget(self.detail)
        lay.addLayout(text, 1)

    def set_state(self, kind: str, title: str, detail: str) -> None:
        self.setProperty("banner", {"neutral": "info"}.get(kind, kind))
        style.repolish(self)
        style.repolish(self.title)
        icon, tone = self.ICONS[kind]
        self.glyph.setPixmap(style.icon(icon, tone).pixmap(QSize(24, 24)))
        self.title.setText(title)
        self.detail.setText(detail)
        self.setAccessibleName(f"{title}. {detail}")


class IntegrityPanel(QWidget):
    """Integrity at a glance (left) and every hash and counter (right)."""

    recomputeRequested = pyqtSignal()
    compareRequested = pyqtSignal()

    FIELDS = [
        ("current", "Pixel SHA-256 (current)"),
        ("original", "Pixel SHA-256 (original)"),
        ("saved", "Pixel SHA-256 (last saved)"),
        ("identical", "Identical to original"),
        ("content", "Modified pixels"),
        ("content_bytes", "Modified channel bytes"),
        ("content_pct", "Changed-pixel percentage"),
        ("positional", "Differs at same (x, y)"),
        ("orientation", "Net transform"),
        ("dims", "Dimensions"),
        ("dims_changed", "Dimensions changed"),
        ("mode", "Mode"),
        ("bpp", "Bytes per pixel"),
        ("history", "Undo history"),
        ("last", "Last operation"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.original_block = StatusBlock("Versus original")
        self.saved_block = StatusBlock("Versus saved file")
        left.addWidget(self.original_block)
        left.addWidget(self.saved_block)
        self.checked = muted("", "faint")
        left.addWidget(self.checked)
        buttons = QHBoxLayout()
        recompute = QPushButton(style.icon("shield"), "Recompute hashes")
        recompute.setToolTip("Hash the whole buffer now (runs in the background)")
        recompute.clicked.connect(self.recomputeRequested)
        copy = QPushButton(style.icon("copy"), "Copy report")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.report_text()))
        compare = QPushButton(style.icon("compare"), "Compare…")
        compare.setToolTip("Compare pixel buffers with another PNG (Ctrl+Shift+D)")
        compare.clicked.connect(self.compareRequested)
        for b in (recompute, copy, compare):
            buttons.addWidget(b)
        buttons.addStretch(1)
        left.addLayout(buttons)
        left.addStretch(1)
        holder = QWidget()
        holder.setLayout(left)
        holder.setMinimumWidth(300)
        holder.setMaximumWidth(460)
        outer.addWidget(holder, 2)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(3)
        self.labels: dict[str, QLabel] = {}
        self.titles: dict[str, str] = {}
        for i, (key, title) in enumerate(self.FIELDS):
            k = muted(title, "key", False)
            v = QLabel(PLACEHOLDER)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setWordWrap(True)
            v.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            if key in ("current", "original", "saved"):
                v.setFont(style.mono_font(0.92))
            grid.addWidget(k, i, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(v, i, 1)
            self.labels[key] = v
            self.titles[key] = title
        grid.setColumnStretch(1, 1)
        right = QWidget()
        right.setLayout(grid)
        outer.addWidget(right, 3)

    def _set(self, key: str, text: str, tone: str | None = None) -> None:
        label = self.labels[key]
        label.setText(text)
        label.setStyleSheet(f"color: {style.tokens()[tone]};" if tone else "")

    def report_text(self) -> str:
        return "\n".join(f"{self.titles[k]}: {self.labels[k].text()}" for k, _ in self.FIELDS)

    def show_document(self, doc: Document | None, hashes: dict | None, checked_at: float | None = None) -> None:
        if doc is None:
            for label in self.labels.values():
                label.setText(PLACEHOLDER)
            self.original_block.set_state("neutral", "No image open", "Open a PNG to see its integrity.")
            self.saved_block.set_state("neutral", PLACEHOLDER, "")
            self.checked.clear()
            return
        state = integrity_state(doc, hashes)
        self.original_block.set_state(*state["original"])
        self.saved_block.set_state(*state["saved"])
        if hashes is None:
            self.checked.setText("Hashing the buffer in the background…")
        elif checked_at is not None:
            self.checked.setText("SHA-256 last computed " + time.strftime("%H:%M:%S", time.localtime(checked_at))
                                 + ". Counters update instantly; hashes follow each edit after a short pause.")
        total = doc.width * doc.height
        self._set("dims", f"{doc.width} × {doc.height} ({total:,} pixels)")
        self._set("mode", f"{doc.mode} (8 bits per channel)")
        self._set("bpp", str(doc.channels))
        self._set("original", doc.original_sha256)
        self._set("saved", doc.saved_pixel_sha256 or "(not saved yet)")
        self._set("orientation", doc.orientation.describe())
        self._set("content", f"{doc.content_diff_pixels:,}", None if doc.content_diff_pixels == 0 else "warning")
        self._set("content_bytes", f"{doc.content_diff_bytes:,}")
        self._set("content_pct", f"{100.0 * doc.content_diff_pixels / total:.6f}%")
        self._set("dims_changed", f"yes ({doc.original.shape[1]}×{doc.original.shape[0]} → {doc.width}×{doc.height})"
                  if doc.dimensions_changed else "no")
        self._set("history", f"{len(doc.history)} step(s), {doc.history.nbytes / 1e6:.1f} MB"
                  + ("  ·  unsaved changes" if doc.dirty else ""))
        self._set("last", doc.last_operation.describe() if doc.last_operation else PLACEHOLDER)
        if hashes is None:
            for key in ("current", "identical", "positional"):
                self._set(key, "computing…", "text_faint")
            return
        self._set("current", hashes["current"])
        same = hashes["current"] == doc.original_sha256 and not doc.dimensions_changed
        self._set("identical", "YES — byte-identical pixel buffer" if same else "no", "success" if same else None)
        positional = hashes.get("positional")
        if positional is None:
            self._set("positional", "n/a (dimensions differ)")
        else:
            self._set("positional", f"{positional[0]:,} pixels, {positional[1]:,} bytes")


# -------------------------------------------------------------------------- history


class HistoryPanel(QWidget):
    """The undo stack as a list: click an entry to undo or redo up to it."""

    jumpRequested = pyqtSignal(int)  # number of applied steps wanted (0 = as opened)
    undoRequested = pyqtSignal()
    redoRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        row = QHBoxLayout()
        self.undo_button = QToolButton()
        self.undo_button.setIcon(style.icon("undo"))
        self.undo_button.setToolTip("Undo (Ctrl+Z)")
        self.undo_button.clicked.connect(self.undoRequested)
        self.redo_button = QToolButton()
        self.redo_button.setIcon(style.icon("redo"))
        self.redo_button.setToolTip("Redo (Ctrl+Shift+Z)")
        self.redo_button.clicked.connect(self.redoRequested)
        self.summary = muted("", "faint", False)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        row.addWidget(self.summary, 1)
        layout.addLayout(row)
        self.list = QListWidget()
        self.list.setIconSize(QSize(16, 16))
        self.list.setAccessibleName("Undo history")
        self.list.itemActivated.connect(self._activated)
        self.list.itemClicked.connect(self._activated)
        layout.addWidget(self.list, 1)
        self._position = 0
        self._saved_row = -1
        self._ids: list = []
        self._doc_key = None

    def show_document(self, doc: Document | None) -> None:
        """Update incrementally: an edit appends one row (and drops redo rows), undo/redo only
        restyles rows, so the cost does not grow with the length of the history."""
        if doc is None:
            self.list.clear()
            self._ids, self._doc_key, self._saved_row = [], None, -1
            self.summary.setText("")
            self.undo_button.setEnabled(False)
            self.redo_button.setEnabled(False)
            return
        h = doc.history
        done, undone = h.undo_stack, h.redo_stack
        commands = [None, *done, *undone]
        ids = [("base", h.base_state)] + [c.id for c in done + undone]
        if self._doc_key is not doc:
            self.list.clear()
            self._ids, self._doc_key = [], doc
        common = 0
        for a, b in zip(self._ids, ids):
            if a != b:
                break
            common += 1
        self.list.blockSignals(True)
        while self.list.count() > common:
            self.list.takeItem(self.list.count() - 1)
        for row in range(common, len(ids)):
            cmd = commands[row]
            if cmd is None:
                text, icon = ("Original image" if h.base_state == 0 else "Earliest kept state"), "image"
            else:
                text, icon = cmd.label, _command_icon(cmd)
            item = QListWidgetItem(style.icon(icon), text)
            item.setData(Qt.ItemDataRole.UserRole, text)
            self.list.addItem(item)
        self._ids = ids
        position = len(done)
        saved_row = next((i for i, x in enumerate(ids) if (x[1] if isinstance(x, tuple) else x) == doc.saved_state),
                         -1) if doc.path is not None else -1
        rows = {saved_row, getattr(self, "_saved_row", -1)}
        lo, hi = sorted((getattr(self, "_position", 0), position))
        rows.update(range(lo, hi + 1))
        rows.update(range(common, len(ids)))
        faint = style.QColor(style.tokens()["text_faint"])
        normal = style.QColor(style.tokens()["text"])
        for row in rows:
            item = self.list.item(row)
            if item is None:
                continue
            applied = row <= position
            font = item.font()
            font.setItalic(not applied)
            item.setFont(font)
            item.setForeground(normal if applied else faint)
            base_text = item.data(Qt.ItemDataRole.UserRole)
            item.setText(base_text + ("   ✓ saved" if row == saved_row else ""))
            item.setToolTip("The saved file holds this state" if row == saved_row else "")
        self._saved_row = saved_row
        self._position = position
        self.list.setCurrentRow(position)
        self.list.scrollToItem(self.list.currentItem())
        self.list.blockSignals(False)
        self.undo_button.setEnabled(h.can_undo)
        self.redo_button.setEnabled(h.can_redo)
        self.summary.setText(f"{len(done)} step{'s' if len(done) != 1 else ''}"
                             + (f" · {len(undone)} redo" if undone else "")
                             + f" · {h.nbytes / 1e6:.1f} of {h.max_bytes / 1e6:.0f} MB")

    def _activated(self, item: QListWidgetItem) -> None:
        row = self.list.row(item)
        if row != self._position:
            self.jumpRequested.emit(row)


def _command_icon(cmd) -> str:
    if isinstance(cmd, TransformCommand):
        return {"rotate_cw": "rotate-cw", "rotate_ccw": "rotate-ccw", "rotate_180": "rotate-180",
                "flip_h": "flip-h", "flip_v": "flip-v"}.get(cmd.op, "rotate-cw")
    if isinstance(cmd, RectPatch):
        return "paste" if cmd.label.startswith("Paste") else "fill"
    if isinstance(cmd, SparsePatch):
        return "pencil" if "stroke" in cmd.label.lower() else "inspect"
    return "dot"


# ---------------------------------------------------------------------- image info


class InfoPanel(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._text = ""

    def toPlainText(self) -> str:  # noqa: N802 (Qt naming)
        return self._text

    def show_document(self, doc: Document | None) -> None:
        t = style.tokens()
        mono = html.escape(style.mono_font().family())
        if doc is None:
            self._text = "No image open."
            self.setHtml(f"<p style='color:{t['text_faint']}'>No image open.</p>")
            return
        rows: list[tuple[str, str]] = [("File", doc.path or "(unsaved)")]
        lay = doc.layout
        if lay is not None:
            ihdr = lay.ihdr
            rows += [("File size", f"{lay.file_size:,} bytes"),
                     ("Source", f"{ihdr.width}×{ihdr.height}, {ihdr.bit_depth}-bit {ihdr.color_type_name}"),
                     ("Interlace", "Adam7" if ihdr.interlace else "none"),
                     ("IDAT", f"{lay.idat_count} chunk(s), {lay.idat_bytes:,} bytes compressed")]
        rows += [("Current", f"{doc.width}×{doc.height} {doc.mode}, {doc.channels} bytes/pixel"),
                 ("Buffer", f"{doc.pixels.nbytes:,} bytes · uint8 (H, W, {doc.channels}) row-major · channels "
                            f"{' '.join(doc.channel_names)} · straight alpha"),
                 ("Orientation", doc.orientation.describe())]
        lines = [f"{k + ':':<13}{v}" for k, v in rows]
        parts = ["<table cellspacing=0 cellpadding=2>"]
        for k, v in rows:
            parts.append(f"<tr><td style='color:{t['text_muted']};padding-right:14px'>{html.escape(k)}</td>"
                         f"<td>{html.escape(v)}</td></tr>")
        parts.append("</table>")
        if lay is not None:
            parts.append(f"<p style='color:{t['text_faint']};margin-top:10px'>CHUNKS (FILE ORDER)</p>"
                         f"<table cellspacing=0 cellpadding=1 style='font-family:\"{mono}\"'>")
            lines += ["", "Chunks (file order):"]
            for name, count, size in lay.summarized_sequence():
                parts.append(f"<tr><td style='padding-right:14px'>{html.escape(name)}</td>"
                             f"<td style='color:{t['text_muted']};padding-right:14px'>{'×' + str(count) if count > 1 else ''}</td>"
                             f"<td align=right>{size:,} bytes</td></tr>")
                lines.append(f"  {name}  ×{count:<4} {size:>12,} bytes" if count > 1 else f"  {name}  {size:>18,} bytes")
            parts.append("</table>")
            text_chunks = [c for c in lay.before_idat + lay.after_idat if c.type == b"tEXt"]
            for c in text_chunks[:20]:
                key, _, value = c.data.partition(b"\0")
                line = f"tEXt {key.decode('latin-1')}: {value[:120].decode('latin-1')}"
                lines.append("  " + line)
                parts.append(f"<p style='font-family:\"{mono}\"'>{html.escape(line)}</p>")
        if doc.warnings:
            parts.append(f"<p style='color:{t['warning']};margin-top:10px'>NOTES</p><ul>")
            lines += ["", "Warnings:"]
            for w in doc.warnings:
                parts.append(f"<li>{html.escape(w)}</li>")
                lines.append(f"  • {w}")
            parts.append("</ul>")
        note = ("The canvas shows raw channel values. No colour profile, gamma or tRNS is applied; alpha is "
                "composited for display only and never stored.")
        parts.append(f"<p style='color:{t['text_faint']};margin-top:10px'>{note}</p>")
        lines += ["", "Display note: " + note]
        self._text = "\n".join(lines)
        self.setHtml("".join(parts))
