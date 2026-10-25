"""Dock panels: pixel/channel inspector, hex byte inspector, image info, integrity."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.document import Document
from ..core.pixels import pixel_offset

PLACEHOLDER = "—"


def mono_font() -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    return font


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


def swatch_style(values) -> str:
    r, g, b = values[:3]
    return f"background: rgb({r},{g},{b}); border: 1px solid #888; min-width: 28px; min-height: 20px;"


@dataclass
class ToolSettings:
    color: tuple[int, int, int, int] = (255, 0, 0, 255)
    size: int = 1
    mask: list[bool] = field(default_factory=lambda: [True, True, True, True])


class ToolOptions(QWidget):
    """Pencil colour (exact values, hex), brush size and channel write mask."""

    changed = pyqtSignal()

    def __init__(self, settings: ToolSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        self.swatch = QToolButton()
        self.swatch.setToolTip("Pick pencil colour")
        self.swatch.clicked.connect(self._pick)
        self.hex_edit = QLineEdit()
        self.hex_edit.setFont(mono_font())
        self.hex_edit.setMaximumWidth(90)
        self.hex_edit.setToolTip("Pencil colour as RRGGBBAA (exact channel values written)")
        self.hex_edit.editingFinished.connect(self._hex_edited)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 64)
        self.size_spin.setPrefix("size ")
        self.size_spin.setToolTip("Square pencil size in pixels")
        self.size_spin.valueChanged.connect(self._size_changed)
        layout.addWidget(QLabel("Pencil"))
        layout.addWidget(self.swatch)
        layout.addWidget(self.hex_edit)
        layout.addWidget(self.size_spin)
        layout.addWidget(QLabel("Write:"))
        self.mask_boxes = []
        for i, name in enumerate("RGBA"):
            box = QCheckBox(name)
            box.setChecked(settings.mask[i])
            box.setToolTip(f"Write the {name} channel (unchecked channels are left untouched)")
            box.toggled.connect(self._mask_changed)
            layout.addWidget(box)
            self.mask_boxes.append(box)
        self._refresh()

    def set_mode(self, mode: str | None) -> None:
        self.mask_boxes[3].setEnabled(mode != "RGB")

    def set_color(self, values) -> None:
        values = tuple(values) + (255,) * (4 - len(values))
        self.settings.color = values[:4]
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        self.swatch.setStyleSheet(swatch_style(self.settings.color))
        self.hex_edit.setText(format_hex(self.settings.color))

    def _pick(self) -> None:
        r, g, b, a = self.settings.color
        color = QColorDialog.getColor(QColor(r, g, b, a), self, "Pencil colour",
                                      QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            self.set_color(color.getRgb())

    def _hex_edited(self) -> None:
        values = parse_hex_color(self.hex_edit.text(), 4)
        if values is None:
            self._refresh()
        else:
            self.set_color(values)

    def _size_changed(self, value: int) -> None:
        self.settings.size = value
        self.changed.emit()

    def _mask_changed(self) -> None:
        self.settings.mask = [b.isChecked() for b in self.mask_boxes]
        self.changed.emit()


class InspectorPanel(QWidget):
    """Shows the inspected pixel's channels in decimal/hex/binary and edits it exactly."""

    applyRequested = pyqtSignal(int, int, object)
    useAsColor = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Document | None = None
        self.pixel: tuple[int, int] | None = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.pos_label = QLabel(PLACEHOLDER)
        self.offset_label = QLabel(PLACEHOLDER)
        self.hex_label = QLabel(PLACEHOLDER)
        for label in (self.pos_label, self.offset_label, self.hex_label):
            label.setFont(mono_font())
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Pixel (x, y):", self.pos_label)
        form.addRow("Byte offset:", self.offset_label)
        form.addRow("Raw bytes:", self.hex_label)
        layout.addLayout(form)

        self.table = QTableWidget(4, 4)
        self.table.setHorizontalHeaderLabels(["Ch", "Dec", "Hex", "Binary"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setFont(mono_font())
        self.table.setMaximumHeight(150)
        layout.addWidget(self.table)

        self.swatch = QLabel()
        self.swatch.setToolTip("RGB of this pixel (alpha not applied)")
        edit_box = QGridLayout()
        self.spins: list[QSpinBox] = []
        for i, name in enumerate("RGBA"):
            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setPrefix(f"{name} ")
            edit_box.addWidget(spin, i // 2, i % 2)
            self.spins.append(spin)
        layout.addLayout(edit_box)
        row = QHBoxLayout()
        self.hex_edit = QLineEdit()
        self.hex_edit.setFont(mono_font())
        self.hex_edit.setPlaceholderText("RRGGBBAA")
        self.hex_edit.editingFinished.connect(self._hex_to_spins)
        self.apply_button = QPushButton("Apply to pixel")
        self.apply_button.setToolTip("Write exactly these values to the inspected pixel (undoable)")
        self.apply_button.clicked.connect(self._apply)
        row.addWidget(self.swatch)
        row.addWidget(self.hex_edit)
        row.addWidget(self.apply_button)
        layout.addLayout(row)
        self.color_button = QPushButton("Use as pencil colour")
        self.color_button.clicked.connect(lambda: self.pixel and self.useAsColor.emit(self.doc.get_pixel(*self.pixel)))
        layout.addWidget(self.color_button)
        layout.addStretch(1)
        self.show_pixel(None, None)

    def show_pixel(self, doc: Document | None, pixel: tuple[int, int] | None) -> None:
        self.doc, self.pixel = doc, pixel
        enabled = doc is not None and pixel is not None
        for w in self.spins + [self.hex_edit, self.apply_button, self.color_button]:
            w.setEnabled(enabled)
        if not enabled:
            self.pos_label.setText(PLACEHOLDER)
            self.offset_label.setText(PLACEHOLDER)
            self.hex_label.setText(PLACEHOLDER)
            self.table.clearContents()
            return
        x, y = pixel
        values = doc.get_pixel(x, y)
        offset = pixel_offset(x, y, doc.width, doc.channels)
        self.pos_label.setText(f"({x}, {y})")
        self.offset_label.setText(f"{offset}  (0x{offset:X})")
        self.hex_label.setText(" ".join(f"{v:02X}" for v in values))
        names = doc.channel_names
        self.table.setRowCount(len(names))
        for row, (name, v) in enumerate(zip(names, values)):
            for col, text in enumerate((name, str(v), f"0x{v:02X}", f"{v:08b}")):
                self.table.setItem(row, col, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        self.swatch.setStyleSheet(swatch_style(values))
        self.spins[3].setVisible(doc.channels == 4)
        for spin, v in zip(self.spins, values):
            spin.setValue(v)
        self.hex_edit.setText(format_hex(values))

    def _hex_to_spins(self) -> None:
        if self.doc is None:
            return
        values = parse_hex_color(self.hex_edit.text(), self.doc.channels)
        if values is not None:
            for spin, v in zip(self.spins, values):
                spin.setValue(v)

    def _apply(self) -> None:
        if self.doc is None or self.pixel is None:
            return
        values = tuple(spin.value() for spin in self.spins[: self.doc.channels])
        self.applyRequested.emit(self.pixel[0], self.pixel[1], values)


class HexPanel(QWidget):
    """Hex dump of the canonical byte stream around the inspected pixel."""

    offsetChosen = pyqtSignal(int)
    LINES = 16
    WIDTH = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.goto = QLineEdit()
        self.goto.setPlaceholderText("offset (decimal or 0x…)")
        self.goto.returnPressed.connect(self._goto)
        self.follow = QCheckBox("Follow cursor")
        self.follow.setToolTip("Track the pixel under the mouse instead of the inspected pixel")
        row.addWidget(self.goto)
        row.addWidget(self.follow)
        layout.addLayout(row)
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(mono_font())
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.view)
        self.caption = QLabel("Canonical bytes: row-major, R G B [A] per pixel")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)

    def show_bytes(self, doc: Document | None, offset: int | None, highlight: int = 0) -> None:
        if doc is None or offset is None:
            self.view.clear()
            return
        flat = doc.pixels.reshape(-1)
        total = flat.size
        start = max(0, (offset // self.WIDTH - self.LINES // 2) * self.WIDTH)
        end = min(total, start + self.LINES * self.WIDTH)
        data = flat[start:end].tobytes()
        lines = []
        for line_start in range(0, len(data), self.WIDTH):
            chunk = data[line_start:line_start + self.WIDTH]
            hex_cells, text_cells = [], []
            for i, b in enumerate(chunk):
                pos = start + line_start + i
                ch = chr(b) if 32 <= b < 127 else "."
                ch = ch.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                if offset <= pos < offset + highlight:
                    hex_cells.append(f'<span style="background:#2e7d32;color:white">{b:02X}</span>')
                    text_cells.append(f'<span style="background:#2e7d32;color:white">{ch}</span>')
                else:
                    hex_cells.append(f"{b:02X}")
                    text_cells.append(ch)
            hex_cells += ["  "] * (self.WIDTH - len(chunk))
            lines.append(f"{start + line_start:010X}  {' '.join(hex_cells)}  {''.join(text_cells)}")
        self.view.setHtml("<pre>" + "\n".join(lines) + "</pre>")

    def _goto(self) -> None:
        text = self.goto.text().strip()
        try:
            value = int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return
        if value >= 0:
            self.offsetChosen.emit(value)


class InfoPanel(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(mono_font())

    def show_document(self, doc: Document | None) -> None:
        if doc is None:
            self.setPlainText("No image open.")
            return
        lines = [
            f"File:        {doc.path or '(unsaved)'}",
        ]
        lay = doc.layout
        if lay is not None:
            ihdr = lay.ihdr
            lines += [
                f"File size:   {lay.file_size:,} bytes",
                f"Source:      {ihdr.width}×{ihdr.height}, {ihdr.bit_depth}-bit {ihdr.color_type_name}",
                f"Interlace:   {'Adam7' if ihdr.interlace else 'none'}",
                f"IDAT:        {lay.idat_count} chunk(s), {lay.idat_bytes:,} bytes compressed",
            ]
        lines += [
            f"Current:     {doc.width}×{doc.height} {doc.mode}, {doc.channels} bytes/pixel",
            f"Buffer:      {doc.pixels.nbytes:,} bytes, uint8 (H, W, {doc.channels}) row-major, "
            f"channels {' '.join(doc.channel_names)}, straight alpha",
            f"Orientation: {doc.orientation.describe()}",
        ]
        if lay is not None:
            lines.append("")
            lines.append("Chunks (file order):")
            for name, count, size in lay.summarized_sequence():
                lines.append(f"  {name}  ×{count:<4} {size:>12,} bytes" if count > 1 else f"  {name}  {size:>18,} bytes")
            text_chunks = [c for c in lay.before_idat + lay.after_idat if c.type == b"tEXt"]
            for c in text_chunks[:20]:
                key, _, value = c.data.partition(b"\0")
                lines.append(f"  tEXt {key.decode('latin-1')}: {value[:120].decode('latin-1')}")
        if doc.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines += [f"  • {w}" for w in doc.warnings]
        lines += [
            "",
            "Display note: the canvas shows raw channel values. No colour profile, gamma or",
            "tRNS is applied; alpha is composited for display only and never stored.",
        ]
        self.setPlainText("\n".join(lines))


class IntegrityPanel(QWidget):
    recomputeRequested = pyqtSignal()

    FIELDS = [
        ("dims", "Dimensions"),
        ("mode", "Mode"),
        ("bpp", "Bytes per pixel"),
        ("current", "Pixel SHA-256 (current)"),
        ("original", "Pixel SHA-256 (original)"),
        ("saved", "Pixel SHA-256 (last saved)"),
        ("identical", "Identical to original"),
        ("orientation", "Net transform"),
        ("content", "Modified pixels"),
        ("content_bytes", "Modified channel bytes"),
        ("content_pct", "Changed-pixel percentage"),
        ("positional", "Differs at same (x, y)"),
        ("dims_changed", "Dimensions changed"),
        ("history", "Undo history"),
        ("last", "Last operation"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.labels: dict[str, QLabel] = {}
        for key, title in self.FIELDS:
            label = QLabel(PLACEHOLDER)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if key in ("current", "original", "saved"):
                label.setFont(mono_font())
            form.addRow(title + ":", label)
            self.labels[key] = label
        layout.addLayout(form)
        button = QPushButton("Recompute hashes now")
        button.clicked.connect(self.recomputeRequested)
        layout.addWidget(button)
        note = QLabel(
            "Hashes cover the raw canonical channel bytes (row-major R,G,B[,A]), not the PNG file. "
            "Saving re-encodes the PNG, so compression and file bytes may change even when every "
            "pixel byte is identical.\n\n"
            "“Modified pixels” compares against the original after applying the net rotate/flip, "
            "so it counts value edits only. “Differs at same (x, y)” is the plain positional "
            "comparison."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)
        layout.addStretch(1)

    def _set(self, key: str, text: str) -> None:
        self.labels[key].setText(text)

    def show_document(self, doc: Document | None, hashes: dict | None) -> None:
        if doc is None:
            for label in self.labels.values():
                label.setText(PLACEHOLDER)
            return
        total = doc.width * doc.height
        self._set("dims", f"{doc.width} × {doc.height} ({total:,} pixels)")
        self._set("mode", f"{doc.mode} (8 bits per channel)")
        self._set("bpp", str(doc.channels))
        self._set("original", doc.original_sha256)
        self._set("saved", doc.saved_pixel_sha256 or "(not saved yet)")
        self._set("orientation", doc.orientation.describe())
        self._set("content", f"{doc.content_diff_pixels:,}")
        self._set("content_bytes", f"{doc.content_diff_bytes:,}")
        self._set("content_pct", f"{100.0 * doc.content_diff_pixels / total:.6f}%")
        self._set("dims_changed", f"YES ({doc.original.shape[1]}×{doc.original.shape[0]} → {doc.width}×{doc.height})"
                  if doc.dimensions_changed else "no")
        self._set("history", f"{len(doc.history)} step(s), {doc.history.nbytes / 1e6:.1f} MB"
                  + ("  •  unsaved changes" if doc.dirty else ""))
        self._set("last", doc.last_operation.describe() if doc.last_operation else PLACEHOLDER)
        if hashes is None:
            self._set("current", "computing…")
            self._set("identical", "computing…")
            self._set("positional", "computing…")
            return
        self._set("current", hashes["current"])
        same = hashes["current"] == doc.original_sha256 and not doc.dimensions_changed
        self._set("identical", "YES — byte-identical pixel buffer" if same else "no")
        positional = hashes.get("positional")
        if positional is None:
            self._set("positional", "n/a (dimensions differ)")
        else:
            self._set("positional", f"{positional[0]:,} pixels, {positional[1]:,} bytes")


def short_path(path: str | None) -> str:
    return os.path.basename(path) if path else "Untitled"
