"""Pixel-difference viewer for two PNGs (or the current buffer against a PNG)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import numpy as np

from ..core.diff import ComparisonMismatch, FileComparison, compare_buffers
from ..core.document import Document, Rect
from ..core.png_chunks import PngFormatError
from ..core.png_io import UnsupportedPngError, load_png, save_png
from .canvas import HeatmapSource, PixelView, zoom_label
from .display import heat_colors
from .panels import mono_font
from .tasks import run_blocking


class DiffDialog(QDialog):
    def __init__(self, parent=None, doc: Document | None = None):
        super().__init__(parent)
        self.setWindowTitle("Compare Pixel Buffers")
        self.resize(1100, 720)
        self.doc = doc
        self.result = None

        grid = QGridLayout()
        self.path_a = QLineEdit()
        self.path_b = QLineEdit()
        browse_a = QPushButton("Browse…")
        browse_b = QPushButton("Browse…")
        browse_a.clicked.connect(lambda: self._browse(self.path_a))
        browse_b.clicked.connect(lambda: self._browse(self.path_b))
        self.use_current = QCheckBox("Use the current (unsaved) buffer as A")
        self.use_current.setEnabled(doc is not None)
        self.use_current.toggled.connect(lambda on: self.path_a.setEnabled(not on))
        if doc is not None and doc.path:
            self.path_a.setText(doc.path)
        grid.addWidget(QLabel("A:"), 0, 0)
        grid.addWidget(self.path_a, 0, 1)
        grid.addWidget(browse_a, 0, 2)
        grid.addWidget(self.use_current, 1, 1)
        grid.addWidget(QLabel("B:"), 2, 0)
        grid.addWidget(self.path_b, 2, 1)
        grid.addWidget(browse_b, 2, 2)

        buttons = QHBoxLayout()
        compare = QPushButton("Compare")
        compare.setDefault(True)
        compare.clicked.connect(self.run_compare)
        self.goto_first = QPushButton("Zoom to first difference")
        self.goto_first.clicked.connect(self._zoom_first)
        self.save_heat = QPushButton("Save heatmap…")
        self.save_heat.clicked.connect(self._save_heatmap)
        self.zoom_label = QLabel()
        for b in (self.goto_first, self.save_heat):
            b.setEnabled(False)
        buttons.addWidget(compare)
        buttons.addWidget(self.goto_first)
        buttons.addWidget(self.save_heat)
        buttons.addStretch(1)
        buttons.addWidget(self.zoom_label)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(mono_font())
        self.view = PixelView()
        self.view.zoomChanged.connect(lambda z: self.zoom_label.setText(f"Heatmap zoom {zoom_label(z)}"))
        self.view.hoverChanged.connect(self._hover)
        legend = QLabel("Heatmap: dark = identical; red → yellow = max channel difference 1 → 255. "
                        "Zoomed out, each screen pixel shows the maximum of the pixels it covers, "
                        "so single changed pixels stay visible. Ctrl/⌘+scroll to zoom.")
        legend.setWordWrap(True)
        self.hover_label = QLabel()
        self.hover_label.setFont(mono_font())
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.view, 1)
        rl.addWidget(self.hover_label)
        rl.addWidget(legend)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.report)
        splitter.addWidget(right)
        splitter.setSizes([420, 680])

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addLayout(buttons)
        layout.addWidget(splitter, 1)
        self._a_pixels = None
        self._b_pixels = None

    def _browse(self, target: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose PNG", target.text(), "PNG images (*.png);;All files (*)")
        if path:
            target.setText(path)

    def _load_and_compare(self, path_a: str | None, path_b: str, a_pixels):
        b = load_png(path_b)
        if a_pixels is None:
            a = load_png(path_a)
            a_pixels, name_a, sha_a = a.pixels, a.path, a.layout.file_sha256
        else:
            name_a, sha_a = "(current buffer)", "(not a file)"
        comparison = FileComparison(name_a, b.path, sha_a, b.layout.file_sha256, None)
        try:
            comparison.diff = compare_buffers(a_pixels, b.pixels)
        except ComparisonMismatch as exc:
            comparison.mismatch = str(exc)
        return comparison, a_pixels, b.pixels

    def run_compare(self) -> None:
        path_b = self.path_b.text().strip()
        use_current = self.use_current.isChecked() and self.doc is not None
        path_a = None if use_current else self.path_a.text().strip()
        if not path_b or (not use_current and not path_a):
            QMessageBox.information(self, "Compare", "Choose two PNG files (or the current buffer and one file).")
            return
        a_pixels = self.doc.pixels.copy() if use_current else None
        try:
            comparison, self._a_pixels, self._b_pixels = run_blocking(
                self, "Decoding and comparing…", self._load_and_compare, path_a, path_b, a_pixels)
        except (UnsupportedPngError, PngFormatError, OSError) as exc:
            QMessageBox.warning(self, "Cannot compare", str(exc))
            return
        self.result = comparison
        lines = comparison.report_lines()
        if use_current:
            lines[2] = "PNG files byte-identical:      n/a (A is an in-memory buffer)"
        self.report.setPlainText("\n".join(lines))
        diff = comparison.diff
        has_diff = diff is not None and not diff.identical
        self.goto_first.setEnabled(has_diff)
        self.save_heat.setEnabled(diff is not None)
        if diff is not None:
            self.view.overlay_rects = [Rect.from_points(*diff.bbox)] if diff.bbox else []
            self.view.set_source(HeatmapSource(diff.heatmap))
            self.view.zoom_to_fit()
        else:
            self.view.set_source(None)

    def _hover(self, pixel) -> None:
        if pixel is None or self.result is None or self.result.diff is None or self._a_pixels is None:
            self.hover_label.clear()
            return
        x, y = pixel
        a = tuple(int(v) for v in self._a_pixels[y, x])
        b = tuple(int(v) for v in self._b_pixels[y, x])
        mark = "=" if a == b else "≠"
        self.hover_label.setText(f"({x}, {y})  A={a} {mark} B={b}")

    def _zoom_first(self) -> None:
        diff = self.result.diff if self.result else None
        if diff and diff.first_diff:
            self.view.set_zoom(16)
            self.view.center_on(diff.first_diff[0] + 0.5, diff.first_diff[1] + 0.5)

    def _save_heatmap(self) -> None:
        diff = self.result.diff if self.result else None
        if diff is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save heatmap", "difference-heatmap.png", "PNG images (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            run_blocking(self, "Saving heatmap…", save_png, path, np.ascontiguousarray(heat_colors(diff.heatmap)))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

