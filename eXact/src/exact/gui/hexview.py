"""Hex view of the canonical byte stream.

The view is virtual: it covers the whole buffer (a 7488×7487 RGBA image is 14 million
rows), but each paint reads only the visible rows straight from ``doc.pixels``. Rows hold
four whole pixels (16 bytes for RGBA, 12 for RGB) so channel columns line up, and each
byte is tinted with its channel colour. Read-only: edits go through the pixel inspector.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PyQt6.QtWidgets import QAbstractScrollArea, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..core.document import Document
from . import style

PIXELS_PER_ROW = 4
_CHANNEL_TOKENS = ("ch_r", "ch_g", "ch_b", "ch_a")


class HexView(QAbstractScrollArea):
    byteActivated = pyqtSignal(int)  # offset chosen by click or keyboard

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Document | None = None
        self.cursor = 0
        self.highlight = (0, 0)  # (offset, length) of the inspected pixel
        self.setFont(style.mono_font())
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.setAccessibleName("Hex bytes")
        self._metrics()

    # -- geometry

    def _metrics(self) -> None:
        fm = QFontMetricsF(self.font())
        self.cw = fm.horizontalAdvance("0")
        self.lh = math.ceil(fm.height() + 4)
        self.ascent = fm.ascent()

    @property
    def channels(self) -> int:
        return self.doc.channels if self.doc is not None else 4

    @property
    def bpr(self) -> int:
        return self.channels * PIXELS_PER_ROW

    @property
    def size_bytes(self) -> int:
        return self.doc.pixels.size if self.doc is not None else 0

    @property
    def total_rows(self) -> int:
        return -(-self.size_bytes // self.bpr)

    def _x_offset(self) -> float:
        return 8 - self.horizontalScrollBar().value()

    def _col_x(self, i: int) -> float:
        """Left edge of the hex cell for byte i of a row (a small gap between pixels)."""
        return self._x_offset() + self.cw * (12 + 3 * i + (i // self.channels))

    def _ascii_x(self) -> float:
        return self._col_x(self.bpr) + self.cw * 1.5

    def _pixel_x(self) -> float:
        return self._ascii_x() + self.cw * (self.bpr + 2)

    def content_width(self) -> int:
        return int(self._pixel_x() + self.horizontalScrollBar().value() + self.cw * 16)

    def visible_rows(self) -> int:
        return max(1, (self.viewport().height() - self.lh) // self.lh)

    def _update_scrollbars(self) -> None:
        vb, hb = self.verticalScrollBar(), self.horizontalScrollBar()
        vb.setRange(0, max(0, self.total_rows - self.visible_rows()))
        vb.setPageStep(self.visible_rows())
        vb.setSingleStep(1)
        hb.setRange(0, max(0, self.content_width() - self.viewport().width()))
        hb.setPageStep(self.viewport().width())

    # -- API

    def set_document(self, doc: Document | None) -> None:
        self.doc = doc
        self.cursor = 0
        self.highlight = (0, 0)
        self._update_scrollbars()
        self.viewport().update()

    def refresh(self) -> None:
        self._update_scrollbars()
        self.viewport().update()

    def show_offset(self, offset: int, length: int, *, move_cursor: bool = True) -> None:
        if self.doc is None:
            return
        offset = min(max(0, offset), self.size_bytes - 1)
        self.highlight = (offset, length)
        if move_cursor:
            self.cursor = offset
        row = offset // self.bpr
        vb = self.verticalScrollBar()
        if not (vb.value() <= row < vb.value() + self.visible_rows()):
            vb.setValue(max(0, row - self.visible_rows() // 2))
        self.viewport().update()

    def describe(self, offset: int) -> str:
        if self.doc is None:
            return ""
        doc = self.doc
        index, channel = divmod(offset, doc.channels)
        x, y = index % doc.width, index // doc.width
        value = int(doc.pixels.reshape(-1)[offset])
        return (f"byte {offset:,} (0x{offset:X}) · pixel ({x}, {y}) · {doc.channel_names[channel]} = {value} "
                f"(0x{value:02X})")

    # -- events

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_scrollbars()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.FontChange:
            self._metrics()
            self._update_scrollbars()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()

    def _offset_at(self, pos) -> int | None:
        if self.doc is None or pos.y() < self.lh:
            return None
        row = self.verticalScrollBar().value() + int((pos.y() - self.lh) // self.lh)
        x = pos.x()
        for i in range(self.bpr):
            if self._col_x(i) - self.cw * 0.5 <= x < self._col_x(i) + self.cw * 2.5:
                break
        else:
            ax = self._ascii_x()
            if ax <= x < ax + self.cw * self.bpr:
                i = int((x - ax) // self.cw)
            else:
                return None
        offset = row * self.bpr + i
        return offset if offset < self.size_bytes else None

    def mousePressEvent(self, event) -> None:
        offset = self._offset_at(event.position())
        if offset is not None:
            self.cursor = offset
            self.byteActivated.emit(offset)
            self.viewport().update()

    def keyPressEvent(self, event) -> None:
        if self.doc is None:
            return super().keyPressEvent(event)
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        page = self.visible_rows() * self.bpr
        moves = {
            Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -self.bpr, Qt.Key.Key_Down: self.bpr,
            Qt.Key.Key_PageUp: -page, Qt.Key.Key_PageDown: page,
        }
        if key in moves:
            target = self.cursor + moves[key]
        elif key == Qt.Key.Key_Home:
            target = 0 if ctrl else self.cursor - self.cursor % self.bpr
        elif key == Qt.Key.Key_End:
            target = self.size_bytes - 1 if ctrl else self.cursor - self.cursor % self.bpr + self.bpr - 1
        elif key == Qt.Key.Key_Tab and not mods:  # jump to the next pixel
            target = self.cursor - self.cursor % self.channels + self.channels
        else:
            return super().keyPressEvent(event)
        target = min(max(0, target), self.size_bytes - 1)
        self.cursor = target
        self.show_offset(target, self.highlight[1] or self.channels, move_cursor=False)
        self.byteActivated.emit(target)

    def focusNextPrevChild(self, forward: bool) -> bool:
        if self.hasFocus() and forward and self.doc is not None:
            return False  # Tab moves the byte cursor; Shift+Tab still leaves the view
        return super().focusNextPrevChild(forward)

    # -- painting

    def paintEvent(self, event) -> None:
        p = QPainter(self.viewport())
        t = style.tokens()
        rect = self.viewport().rect()
        p.fillRect(rect, QColor(t["input"]))
        p.setFont(self.font())
        if self.doc is None:
            p.setPen(QColor(t["text_faint"]))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No image open")
            p.end()
            return
        doc, bpr, ch = self.doc, self.bpr, self.channels
        flat = doc.pixels.reshape(-1)
        first = self.verticalScrollBar().value()
        rows = self.visible_rows() + 1
        start = first * bpr
        data = flat[start:min(self.size_bytes, start + rows * bpr)].tobytes()
        hl_start, hl_len = self.highlight
        channel_colors = [QColor(t[_CHANNEL_TOKENS[c]]) for c in range(ch)]
        muted, faint, text = QColor(t["text_muted"]), QColor(t["text_faint"]), QColor(t["text"])
        sel = QColor(t["selection"])
        accent = QColor(t["accent"])

        # header
        p.fillRect(QRect(0, 0, rect.width(), self.lh), QColor(t["panel"]))
        p.setPen(faint)
        p.drawText(QRectF(self._x_offset(), 0, self.cw * 11, self.lh), Qt.AlignmentFlag.AlignVCenter, "Offset")
        for i in range(bpr):
            p.setPen(channel_colors[i % ch])
            p.drawText(QRectF(self._col_x(i), 0, self.cw * 2, self.lh), Qt.AlignmentFlag.AlignVCenter,
                       f" {doc.channel_names[i % ch]}")
        p.setPen(faint)
        p.drawText(QRectF(self._ascii_x(), 0, self.cw * bpr, self.lh), Qt.AlignmentFlag.AlignVCenter, "ASCII")
        p.drawText(QRectF(self._pixel_x(), 0, self.cw * 16, self.lh), Qt.AlignmentFlag.AlignVCenter, "First pixel")
        p.setPen(QPen(QColor(t["border"])))
        p.drawLine(0, self.lh - 1, rect.width(), self.lh - 1)

        for r in range(rows):
            row_start = start + r * bpr
            if row_start >= self.size_bytes:
                break
            y = self.lh * (r + 1)
            chunk = data[r * bpr:(r + 1) * bpr]
            p.setPen(faint)
            p.drawText(QRectF(self._x_offset(), y, self.cw * 11, self.lh), Qt.AlignmentFlag.AlignVCenter,
                       f"{row_start:010X}")
            ascii_x = self._ascii_x()
            for i, b in enumerate(chunk):
                off = row_start + i
                cell = QRectF(self._col_x(i) - self.cw * 0.25, y + 1, self.cw * 2.5, self.lh - 2)
                acell = QRectF(ascii_x + self.cw * i, y + 1, self.cw, self.lh - 2)
                if hl_start <= off < hl_start + hl_len:
                    p.fillRect(cell, sel)
                    p.fillRect(acell, sel)
                if off == self.cursor:
                    p.setPen(QPen(accent, 1.5))
                    p.drawRect(cell.adjusted(0.5, 0.5, -0.5, -0.5))
                p.setPen(channel_colors[i % ch] if b else muted)
                p.drawText(QRectF(self._col_x(i), y, self.cw * 2, self.lh), Qt.AlignmentFlag.AlignVCenter, f"{b:02X}")
                p.setPen(text if 32 <= b < 127 else faint)
                p.drawText(acell, Qt.AlignmentFlag.AlignVCenter, chr(b) if 32 <= b < 127 else "·")
            index = row_start // ch
            p.setPen(faint)
            p.drawText(QRectF(self._pixel_x(), y, self.cw * 18, self.lh), Qt.AlignmentFlag.AlignVCenter,
                       f"({index % doc.width}, {index // doc.width})")
        if self.hasFocus():
            p.setPen(QPen(accent, 1))
            p.drawRect(rect.adjusted(0, 0, -1, -1))
        p.end()


class HexPanel(QWidget):
    """Hex view plus a go-to field and a follow-hover toggle."""

    offsetChosen = pyqtSignal(int)  # navigate to this byte (inspects its pixel)
    pixelChosen = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        row = QHBoxLayout()
        self.goto = QLineEdit()
        self.goto.setPlaceholderText("Go to offset (1234, 0x4D2) or pixel x, y")
        self.goto.setClearButtonEnabled(True)
        self.goto.setMinimumWidth(280)
        self.goto.setMaximumWidth(380)
        self.goto.returnPressed.connect(self._goto)
        self.follow = QCheckBox("Follow hover")
        self.follow.setToolTip("Track the pixel under the mouse instead of the inspected pixel")
        self.info = QLabel("")
        self.info.setProperty("role", "muted")
        self.info.setFont(style.mono_font(0.95))
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.goto)
        row.addWidget(self.follow)
        row.addSpacing(12)
        row.addWidget(self.info, 1)
        layout.addLayout(row)
        self.view = HexView()
        self.view.byteActivated.connect(self._activated)
        layout.addWidget(self.view, 1)
        self.caption = QLabel("Canonical bytes: row-major, R G B [A] per pixel, exactly as stored and hashed. "
                              "Arrows, Page Up/Down, Home/End and Tab (next pixel) navigate.")
        self.caption.setProperty("role", "faint")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)

    def set_document(self, doc: Document | None) -> None:
        self.view.set_document(doc)
        self.info.setText("")

    def show_bytes(self, doc: Document | None, offset: int | None, highlight: int = 0) -> None:
        if doc is None or offset is None:
            self.view.set_document(doc)
            return
        if self.view.doc is not doc:
            self.view.set_document(doc)
        self.view.show_offset(offset, highlight)
        self.info.setText(self.view.describe(offset))

    def _activated(self, offset: int) -> None:
        self.info.setText(self.view.describe(offset))
        self.offsetChosen.emit(offset)

    def _goto(self) -> None:
        text = self.goto.text().strip().strip("()")
        if "," in text:
            try:
                x, y = (int(v) for v in text.split(","))
            except ValueError:
                self._invalid()
                return
            self.pixelChosen.emit(x, y)
            return
        try:
            value = int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            self._invalid()
            return
        if value >= 0:
            self.offsetChosen.emit(value)

    def _invalid(self) -> None:
        self.goto.setProperty("invalid", "true")
        style.repolish(self.goto)
        self.goto.textEdited.connect(self._valid_again)

    def _valid_again(self) -> None:
        self.goto.setProperty("invalid", "false")
        style.repolish(self.goto)
        try:
            self.goto.textEdited.disconnect(self._valid_again)
        except TypeError:
            pass
