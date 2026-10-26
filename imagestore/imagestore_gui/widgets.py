"""Building blocks for the ImageStore window: drop zones, stat tiles, banners, progress."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QPainter
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import imagestore as core

from . import style


def label(text: str = "", role: str | None = None, *, wrap: bool = False, selectable: bool = False,
          mono: bool = False) -> QLabel:
    w = QLabel(text)
    if role:
        style.set_role(w, role)
    w.setWordWrap(wrap)
    if selectable:
        w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if mono:
        w.setFont(style.mono_font())
    return w


def card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("role", "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    return frame, layout


def button(text: str, icon: str | None = None, variant: str | None = None) -> QPushButton:
    b = QPushButton(text)
    if icon:
        b.setIcon(style.icon(icon))
    if variant:
        b.setProperty("variant", variant)
    return b


class Runner(QObject):
    """Runs one ImageStore operation on a worker thread with progress and cancellation.

    The operation is called as fn(*args, task=Task, **kwargs); results come back on the GUI
    thread through the signals. Progress updates are throttled so a fast operation cannot
    flood the event loop.
    """

    progress = pyqtSignal(str, object, object)  # label, done, total (Python ints: may exceed 2**31)
    finished = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None
        self._last = (None, 0.0)

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, fn, *args, **kwargs) -> None:
        if self.busy:
            raise RuntimeError("an operation is already running")
        self._cancel = threading.Event()
        task = core.Task(self._report, self._cancel)

        def work():
            try:
                try:
                    result = fn(*args, task=task, **kwargs)
                except BaseException as exc:  # reported on the GUI thread
                    self.failed.emit(exc)
                else:
                    self.finished.emit(result)
            except RuntimeError:  # the window closed meanwhile: nobody to tell
                pass

        self._thread = threading.Thread(target=work, name="imagestore-op", daemon=True)
        self._thread.start()

    def _report(self, label: str, done: int, total: int | None) -> None:
        now = time.monotonic()
        last_label, last_time = self._last
        if label != last_label or now - last_time >= 0.05 or (total and done >= total):
            self._last = (label, now)
            try:
                self.progress.emit(label, done, total)
            except RuntimeError:  # window gone: stop the operation instead of failing to report
                self.cancel()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.busy


class DropZone(QFrame):
    """A dashed drop target with a call to action. Emits the dropped/chosen local paths."""

    dropped = pyqtSignal(list)

    def __init__(self, icon: str, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.setProperty("role", "dropzone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)
        self.accepts = lambda path: True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(6)
        layout.addStretch(1)
        glyph = QLabel()
        glyph.setPixmap(style.icon(icon).pixmap(QSize(36, 36)))
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(glyph)
        self.title = label(title, "title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        self.subtitle = label(subtitle, "muted", wrap=True)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.subtitle)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)
        self._buttons_end = self.buttons.count()
        self.buttons.addStretch(1)
        layout.addSpacing(6)
        layout.addLayout(self.buttons)
        layout.addStretch(1)

    def add_button(self, b: QPushButton) -> QPushButton:
        self.buttons.insertWidget(self._buttons_end, b)
        self._buttons_end += 1
        return b

    def _set_active(self, on: bool) -> None:
        self.setProperty("active", "true" if on else "false")
        style.repolish(self)

    @staticmethod
    def _paths(event) -> list[Path]:
        md = event.mimeData()
        if not md.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in md.urls() if u.isLocalFile()]

    def dragEnterEvent(self, event) -> None:
        paths = self._paths(event)
        if paths and all(self.accepts(p) for p in paths[:1]):
            event.acceptProposedAction()
            self._set_active(True)

    def dragLeaveEvent(self, event) -> None:
        self._set_active(False)

    def dropEvent(self, event) -> None:
        self._set_active(False)
        paths = self._paths(event)
        if paths:
            event.acceptProposedAction()
            self.dropped.emit(paths)


class ElidedLabel(QLabel):
    """A single-line label that elides in the middle (for long paths); full text in the tooltip."""

    def __init__(self, text: str = "", role: str | None = None, parent=None):
        super().__init__(parent)
        self._full = ""
        if role:
            style.set_role(self, role)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(40, self.width())))


class SourceCard(QFrame):
    """The chosen input, shown compactly in place of the drop zone."""

    cleared = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "card")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 10, 12)
        row.setSpacing(12)
        self.glyph = QLabel()
        row.addWidget(self.glyph)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name = label("", "title")
        self.detail = ElidedLabel("", "muted")
        text.addWidget(self.name)
        text.addWidget(self.detail)
        row.addLayout(text, 1)
        self.change = button("Change…", variant="flat")
        row.addWidget(self.change)
        clear = QToolButton()
        clear.setIcon(style.icon("close"))
        clear.setToolTip("Clear")
        clear.setAccessibleName("Clear selection")
        clear.clicked.connect(self.cleared)
        row.addWidget(clear)

    def show_source(self, icon: str, name: str, detail: str) -> None:
        self.glyph.setPixmap(style.icon(icon).pixmap(QSize(28, 28)))
        self.name.setText(name)
        self.detail.set_full_text(detail)


class StatTile(QFrame):
    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        self.caption = label(caption, "section")
        self.value = label("—", "title", selectable=True)
        self.sub = label("", "faint", wrap=True)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        layout.addWidget(self.sub)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set(self, value: str, sub: str = "") -> None:
        self.value.setText(value)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))


class SizeBars(QWidget):
    """Original vs stored payload vs PNG file, as proportional bars (display only)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[tuple[str, int, str]] = []
        self.setMinimumHeight(78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_sizes(self, rows: list[tuple[str, int, str]]) -> None:
        self.rows = rows
        self.setAccessibleDescription("; ".join(f"{name} {core.human(n)}" for name, n, _ in rows))
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(400, 26 * max(1, len(self.rows)))

    def paintEvent(self, event) -> None:
        if not self.rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = self.fontMetrics()
        name_w = max(fm.horizontalAdvance(name) for name, _, _ in self.rows) + 12
        size_w = fm.horizontalAdvance("0000.00 MiB") + 8
        biggest = max(1, max(n for _, n, _ in self.rows))
        row_h = self.height() / len(self.rows)
        bar_w = max(10, self.width() - name_w - size_w)
        for i, (name, n, token) in enumerate(self.rows):
            y = i * row_h
            p.setPen(style.color("text_muted"))
            p.drawText(QRectF(0, y, name_w, row_h), Qt.AlignmentFlag.AlignVCenter, name)
            track = QRectF(name_w, y + row_h / 2 - 4, bar_w, 8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(style.color("input"))
            p.drawRoundedRect(track, 4, 4)
            fill = QRectF(track)
            fill.setWidth(max(3.0, bar_w * n / biggest) if n else 0)
            p.setBrush(QColor(style.tokens()[token]))
            p.drawRoundedRect(fill, 4, 4)
            p.setPen(style.color("text"))
            p.drawText(QRectF(name_w + bar_w + 8, y, size_w, row_h), Qt.AlignmentFlag.AlignVCenter, core.human(n))
        p.end()


class Banner(QFrame):
    """Status banner: ok / warn / error / info. Errors carry a hint and expandable detail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)
        self.glyph = QLabel()
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(self.glyph)
        body = QVBoxLayout()
        body.setSpacing(4)
        self.title = label("", "title", wrap=True)
        self.hint = label("", wrap=True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        self.detail = label("", "muted", wrap=True, selectable=True, mono=True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.hide()
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.toggle = button("Show details", "chevron-right", "flat")
        self.toggle.clicked.connect(self._toggle)
        self.copy = button("Copy details", "copy", "flat")
        self.copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._copy_text))
        self.copy.hide()
        actions.addWidget(self.toggle)
        actions.addWidget(self.copy)
        actions.addStretch(1)
        body.addWidget(self.title)
        body.addWidget(self.hint)
        body.addWidget(self.detail)
        body.addLayout(actions)
        outer.addLayout(body, 1)
        self._copy_text = ""
        self.hide()

    def show_message(self, kind: str, title: str, hint: str = "", detail: str = "") -> None:
        icons = {"ok": ("check-circle", "success"), "warn": ("alert", "warning"),
                 "error": ("x-circle", "danger"), "info": ("info", "accent")}
        name, tone = icons[kind]
        self.setProperty("banner", kind)
        style.repolish(self)
        for child in self.findChildren(QLabel):
            style.repolish(child)
        self.glyph.setPixmap(style.icon(name, tone).pixmap(QSize(22, 22)))
        self.title.setText(title)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.detail.setText(detail)
        self.detail.hide()
        self._copy_text = f"{title}\n{hint}\n\n{detail}".strip()
        self.toggle.setVisible(bool(detail))
        self.toggle.setText("Show details")
        self.toggle.setIcon(style.icon("chevron-right"))
        self.copy.hide()
        self.setAccessibleName(f"{title}. {hint}")
        self.show()

    def show_error(self, exc: BaseException) -> None:
        ex = core.explain(exc)
        kind = "info" if getattr(exc, "kind", "") == "cancelled" else "error"
        self.show_message(kind, ex.title, ex.hint, ex.detail)

    def _toggle(self) -> None:
        shown = not self.detail.isVisible()
        self.detail.setVisible(shown)
        self.copy.setVisible(shown)
        self.toggle.setText("Hide details" if shown else "Show details")
        self.toggle.setIcon(style.icon("chevron-down" if shown else "chevron-right"))


class ProgressRow(QWidget):
    """Phase label, determinate or pulsing bar, byte counter and a Cancel button."""

    cancelRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(4)
        self.phase = label("", "muted")
        self.amount = label("", "faint")
        self.amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.cancel = button("Cancel", "stop")
        self.cancel.setToolTip("Stop safely; partial output is removed (Esc)")
        self.cancel.clicked.connect(self.cancelRequested)
        layout.addWidget(self.phase, 0, 0)
        layout.addWidget(self.amount, 0, 1)
        layout.addWidget(self.bar, 1, 0, 1, 2)
        layout.addWidget(self.cancel, 0, 2, 2, 1)
        layout.setColumnStretch(0, 1)
        self.hide()

    def begin(self, text: str = "Starting…") -> None:
        self.phase.setText(text)
        self.amount.setText("")
        self.bar.setRange(0, 0)
        self.cancel.setEnabled(True)
        self.cancel.setText("Cancel")
        self.show()

    def update_progress(self, text: str, done: int, total: int | None) -> None:
        self.phase.setText(text + "…")
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * min(done, total) / total))
            self.amount.setText(f"{core.human(done)} of {core.human(total)}")
        else:
            self.bar.setRange(0, 0)
            self.amount.setText(f"{done:,} entries" if done and text.startswith("Packing folder") else "")

    def cancelling(self) -> None:
        self.cancel.setEnabled(False)
        self.cancel.setText("Cancelling…")

    def end(self) -> None:
        self.hide()


class Segmented(QFrame):
    """A compact segmented control (exclusive checkable buttons)."""

    changed = pyqtSignal(str)

    def __init__(self, items: list[tuple[str, str, str]], parent=None):
        super().__init__(parent)
        self.setProperty("role", "segmented")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QToolButton] = {}
        for key, text, icon in items:
            b = QToolButton()
            b.setText(text)
            b.setIcon(style.icon(icon))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.group.addButton(b)
            self.buttons[key] = b
            layout.addWidget(b)
            b.toggled.connect(lambda on, k=key: on and self.changed.emit(k))
        next(iter(self.buttons.values())).setChecked(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def value(self) -> str:
        return next(k for k, b in self.buttons.items() if b.isChecked())

    def set_value(self, key: str) -> None:
        self.buttons[key].setChecked(True)


class KeyValueGrid(QWidget):
    """Two-column key/value list with optional status glyphs (for metadata)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(7)
        self.grid.setColumnStretch(1, 1)
        self.values: dict[str, QLabel] = {}

    def clear(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.values = {}

    def add(self, key: str, value: str, *, mono: bool = False, tone: str | None = None) -> QLabel:
        row = self.grid.rowCount()
        k = label(key, "key")
        k.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        v = label(value, "value", wrap=True, selectable=True, mono=mono)
        v.setTextFormat(Qt.TextFormat.PlainText)
        if tone:
            v.setStyleSheet(f"color: {style.tokens()[tone]}; font-weight: 600;")
        self.grid.addWidget(k, row, 0)
        self.grid.addWidget(v, row, 1)
        self.values[key] = v
        return v
