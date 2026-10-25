"""Pixel-difference viewer for two PNGs (or the current buffer against a PNG)."""

from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.diff import ComparisonMismatch, FileComparison, compare_buffers
from ..core.document import Document, Rect
from ..core.png_io import load_png, save_png
from . import style
from .canvas import HeatmapSource, PixelView, zoom_label
from .display import heat_colors
from .errors import show_error
from .panels import StatusBlock, muted, section
from .tasks import TaskCancelled, run_blocking


class DiffDialog(QDialog):
    def __init__(self, parent=None, doc: Document | None = None, *, path_b: str | None = None,
                 use_current: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Compare Pixel Buffers")
        self.resize(1180, 760)
        self.doc = doc
        self.result = None
        self.setAcceptDrops(True)

        inputs = QFrame()
        inputs.setProperty("role", "card")
        grid = QGridLayout(inputs)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(10)
        self.path_a = QLineEdit()
        self.path_b = QLineEdit()
        for edit, name in ((self.path_a, "A"), (self.path_b, "B")):
            edit.setPlaceholderText(f"PNG file {name} (or drop a file on this window)")
            edit.setAccessibleName(f"Image {name}")
            edit.returnPressed.connect(self.run_compare)
        browse_a = QPushButton(style.icon("open"), "Browse…")
        browse_b = QPushButton(style.icon("open"), "Browse…")
        browse_a.clicked.connect(lambda: self._browse(self.path_a))
        browse_b.clicked.connect(lambda: self._browse(self.path_b))
        self.use_current = QCheckBox("Use the current (unsaved) buffer as A")
        self.use_current.setEnabled(doc is not None)
        self.use_current.toggled.connect(lambda on: (self.path_a.setEnabled(not on), browse_a.setEnabled(not on)))
        if doc is not None and doc.path:
            self.path_a.setText(doc.path)
        if path_b:
            self.path_b.setText(path_b)
        self.use_current.setChecked(use_current and doc is not None)
        compare = QPushButton(style.icon("compare"), "Compare")
        compare.setProperty("variant", "primary")
        compare.setDefault(True)
        compare.clicked.connect(self.run_compare)
        grid.addWidget(QLabel("A"), 0, 0)
        grid.addWidget(self.path_a, 0, 1)
        grid.addWidget(browse_a, 0, 2)
        grid.addWidget(self.use_current, 1, 1)
        grid.addWidget(QLabel("B"), 2, 0)
        grid.addWidget(self.path_b, 2, 1)
        grid.addWidget(browse_b, 2, 2)
        grid.addWidget(compare, 0, 3, 3, 1)
        grid.setColumnStretch(1, 1)

        # left: verdict + metrics + raw report
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)
        self.verdict = StatusBlock("Result")
        self.verdict.set_state("neutral", "Nothing compared yet",
                               "Choose two PNGs, or the current buffer and a PNG, then Compare.")
        ll.addWidget(self.verdict)
        ll.addWidget(section("Levels of sameness"))
        self.levels = QGridLayout()
        self.levels.setVerticalSpacing(6)
        self.level_pills: dict[str, QLabel] = {}
        for row, (key, text) in enumerate((("files", "PNG files byte-identical"),
                                           ("pixels", "Decoded pixel buffers identical"),
                                           ("visual", "Visually identical"))):
            self.levels.addWidget(muted(text, "key", False), row, 0)
            pill = QLabel("—")
            self.levels.addWidget(pill, row, 1, Qt.AlignmentFlag.AlignLeft)
            self.level_pills[key] = pill
        self.levels.setColumnStretch(1, 1)
        ll.addLayout(self.levels)
        ll.addWidget(section("Full report"))
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(style.mono_font(0.92))
        self.report.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        ll.addWidget(self.report, 1)

        # right: heatmap
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        bar = QHBoxLayout()
        bar.addWidget(section("Difference heatmap"))
        bar.addStretch(1)
        self.zoom_label = muted("", "faint", False)
        bar.addWidget(self.zoom_label)
        self.goto_first = QPushButton(style.icon("goto"), "First difference")
        self.goto_first.clicked.connect(self._zoom_first)
        self.fit = QPushButton(style.icon("zoom-fit"), "Fit")
        self.fit.clicked.connect(lambda: self.view.zoom_to_fit())
        self.save_heat = QPushButton(style.icon("save"), "Save heatmap…")
        self.save_heat.clicked.connect(self._save_heatmap)
        for b in (self.goto_first, self.fit, self.save_heat):
            b.setEnabled(False)
            bar.addWidget(b)
        rl.addLayout(bar)
        self.view = PixelView()
        self.view.zoomChanged.connect(lambda z: self.zoom_label.setText(zoom_label(z)))
        self.view.hoverChanged.connect(self._hover)
        rl.addWidget(self.view, 1)
        self.hover_label = QLabel(" ")
        self.hover_label.setFont(style.mono_font(0.95))
        rl.addWidget(self.hover_label)
        rl.addWidget(muted("Dark = identical; red → yellow = largest channel difference 1 → 255. Zoomed out, each "
                           "screen pixel shows the maximum of the pixels it covers, so a single changed pixel stays "
                           "visible. Ctrl+scroll zooms.", "faint"))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([440, 740])
        splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)
        layout.addWidget(inputs)
        layout.addWidget(splitter, 1)
        self._a_pixels = None
        self._b_pixels = None

    def _browse(self, target: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose PNG", target.text(), "PNG images (*.png);;All files (*)")
        if path:
            target.setText(path)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if len(paths) >= 2:
            self.use_current.setChecked(False)
            self.path_a.setText(paths[0])
            self.path_b.setText(paths[1])
        elif paths:
            target = self.path_b if (self.use_current.isChecked() or self.path_a.text()) else self.path_a
            target.setText(paths[0])

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
            self.verdict.set_state("info", "Choose what to compare",
                                   "Pick two PNG files, or tick the current buffer and pick one file.")
            return
        a_pixels = self.doc.pixels.copy() if use_current else None
        try:
            comparison, self._a_pixels, self._b_pixels = run_blocking(
                self, "Decoding and comparing…", self._load_and_compare, path_a, path_b, a_pixels, cancellable=True)
        except TaskCancelled:
            self.verdict.set_state("info", "Comparison cancelled", "Nothing was changed.")
            return
        except Exception as exc:
            show_error(self, exc, "compare")
            return
        self.result = comparison
        lines = comparison.report_lines()
        if use_current:
            lines[2] = "PNG files byte-identical:      n/a (A is an in-memory buffer)"
        self.report.setPlainText("\n".join(lines))
        diff = comparison.diff
        if diff is None:
            self.verdict.set_state("error", "Not comparable", comparison.mismatch or "")
            for pill in self.level_pills.values():
                style.set_pill(pill, "neutral", "n/a")
        else:
            if diff.identical:
                self.verdict.set_state("ok", "Pixel buffers are identical",
                                       f"All {diff.total_bytes:,} channel bytes match (SHA-256 {diff.sha256_a[:16]}…).")
            else:
                where = f"First difference at {diff.first_diff}." if diff.first_diff else ""
                self.verdict.set_state(
                    "warn", f"{diff.changed_pixels:,} pixel{'s' if diff.changed_pixels != 1 else ''} differ",
                    f"{diff.changed_bytes:,} channel bytes changed; {diff.match_percent:.6g}% of pixels match. "
                    + where + (f" {diff.invisible_pixels:,} change(s) are invisible (RGB under alpha 0)."
                               if diff.invisible_pixels else ""))
            if use_current:
                style.set_pill(self.level_pills["files"], "neutral", "n/a")
            else:
                same = comparison.files_identical
                style.set_pill(self.level_pills["files"], "ok" if same else "neutral", "yes" if same else "no")
            style.set_pill(self.level_pills["pixels"], "ok" if diff.identical else "warn",
                           "yes" if diff.identical else "no")
            style.set_pill(self.level_pills["visual"], "ok" if diff.visually_identical else "warn",
                           "yes" if diff.visually_identical else "no")
        has_diff = diff is not None and not diff.identical
        self.goto_first.setEnabled(has_diff)
        self.save_heat.setEnabled(diff is not None)
        self.fit.setEnabled(diff is not None)
        if diff is not None:
            self.view.overlay_rects = [Rect.from_points(*diff.bbox)] if diff.bbox else []
            self.view.set_source(HeatmapSource(diff.heatmap))
            self.view.zoom_to_fit()
        else:
            self.view.set_source(None)

    def _hover(self, pixel) -> None:
        if pixel is None or self.result is None or self.result.diff is None or self._a_pixels is None:
            self.hover_label.setText(" ")
            return
        x, y = pixel
        a = tuple(int(v) for v in self._a_pixels[y, x])
        b = tuple(int(v) for v in self._b_pixels[y, x])
        mark = "=" if a == b else "≠"
        self.hover_label.setText(f"({x}, {y})  A {a} {mark} B {b}")

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
            show_error(self, exc, "save")
