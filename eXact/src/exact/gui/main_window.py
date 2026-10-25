"""Main editor window."""

from __future__ import annotations

import os
import threading

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSlider,
    QSpinBox,
)

from .. import MOTTO, TAGLINE, __version__
from ..core import transforms
from ..core.document import ChannelMismatchError, Clip, Document, OperationSummary, Rect
from ..core.history import TransformCommand
from ..core.pixels import pixel_offset
from ..core.png_chunks import PngFormatError
from ..core.png_io import SaveVerificationError, UnsupportedPngError
from .canvas import ZOOM_LEVELS, EditorCanvas, zoom_label
from .diff_dialog import DiffDialog
from .display import DISPLAY_MODES
from .panels import HexPanel, InfoPanel, InspectorPanel, IntegrityPanel, ToolOptions, ToolSettings, short_path
from .tasks import run_blocking

HEAVY_PIXELS = 4_000_000
GUARANTEES_TEXT = """\
<h3>What eXact guarantees</h3>
<ul>
<li>Pixels you did not explicitly edit keep exactly the same R, G, B (and A) bytes.</li>
<li>Edits <i>replace</i> channel values; nothing is blended, premultiplied or colour-managed.
    Writing into a transparent pixel changes only that pixel's selected channels.</li>
<li>Rotations and flips move whole pixels without changing any value; the inverse
    transform restores the original buffer byte-for-byte.</li>
<li>Every save is decoded again and compared byte-for-byte with the buffer before the
    original file is atomically replaced. If verification fails, the original is untouched.</li>
</ul>
<h3>What it does not guarantee</h3>
<ul>
<li>The PNG <i>file</i> bytes: compression is redone, so file size and SHA-256 differ
    even when every pixel byte is identical.</li>
<li>Decodability of pixel-order payloads after a rotation/flip — only after the inverse.</li>
<li>Animated PNG frames, trailing data after IEND, digital signatures, and unknown
    unsafe-to-copy chunks after pixel edits (these are listed before saving).</li>
</ul>
<p>Supported: 8-bit RGB and 8-bit RGBA PNG. Everything else is refused, never converted.</p>
"""


class IntegrityWorker(QObject):
    done = pyqtSignal(object, int, object)

    def __init__(self):
        super().__init__()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, doc: Document) -> bool:
        if self._running:
            return False
        self._running = True
        generation = doc.generation

        def work():
            try:
                hashes = {"current": doc.current_sha256(), "positional": doc.positional_diff()}
            except Exception as exc:  # buffer replaced mid-scan; caller retries
                hashes = {"error": str(exc)}
            self._running = False
            self.done.emit(doc, generation, hashes)

        threading.Thread(target=work, name="exact-integrity", daemon=True).start()
        return True


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self.settings = settings or QSettings("eXact", "eXact")
        self.doc: Document | None = None
        self.clipboard: Clip | None = None
        self.hashes: dict | None = None
        self.tool_settings = ToolSettings()
        self.resize(1400, 900)
        self.setAcceptDrops(True)

        self.canvas = EditorCanvas(self.tool_settings)
        self.setCentralWidget(self.canvas)
        self.inspector = InspectorPanel()
        self.hex_panel = HexPanel()
        self.info_panel = InfoPanel()
        self.integrity_panel = IntegrityPanel()
        self.docks = {}
        for key, title, widget, area in (
            ("inspector", "Pixel Inspector", self.inspector, Qt.DockWidgetArea.RightDockWidgetArea),
            ("integrity", "Integrity", self.integrity_panel, Qt.DockWidgetArea.RightDockWidgetArea),
            ("hex", "Hex Bytes", self.hex_panel, Qt.DockWidgetArea.BottomDockWidgetArea),
            ("info", "Image Info", self.info_panel, Qt.DockWidgetArea.BottomDockWidgetArea),
        ):
            dock = QDockWidget(title, self)
            dock.setObjectName(key)
            if area == Qt.DockWidgetArea.RightDockWidgetArea:
                scroller = QScrollArea()
                scroller.setWidgetResizable(True)
                scroller.setFrameShape(QScrollArea.Shape.NoFrame)
                scroller.setWidget(widget)
                widget = scroller
            dock.setWidget(widget)
            self.addDockWidget(area, dock)
            self.docks[key] = dock
        self.tabifyDockWidget(self.docks["hex"], self.docks["info"])
        self.docks["hex"].raise_()
        self.resizeDocks([self.docks["inspector"]], [460], Qt.Orientation.Horizontal)
        self.resizeDocks([self.docks["hex"]], [260], Qt.Orientation.Vertical)

        self.worker = IntegrityWorker()
        self.worker.done.connect(self._hashes_ready)
        self.hash_timer = QTimer(self)
        self.hash_timer.setSingleShot(True)
        self.hash_timer.setInterval(500)
        self.hash_timer.timeout.connect(self._start_hashing)

        self._build_actions()
        self._build_menus()
        self._build_toolbars()
        self._build_statusbar()
        self._connect()
        self.set_document(None)

    def _action(self, text, slot=None, shortcut=None, checkable=False, tip=None) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(checkable)
        if tip:
            action.setStatusTip(tip)
            action.setToolTip(tip)
        if slot is not None:
            action.triggered.connect(slot)
        return action

    def _build_actions(self) -> None:
        a = self._action
        S = QKeySequence.StandardKey
        self.act_open = a("&Open…", self.open_dialog, S.Open)
        self.act_save = a("&Save", self.save, S.Save, tip="Encode, verify by re-decoding, then atomically replace")
        self.act_save_as = a("Save &As…", self.save_as, S.SaveAs)
        self.act_compare = a("Compare PNG Pixel Buffers…", self.compare_dialog, "Ctrl+Shift+D")
        self.act_quit = a("&Quit", self.close, S.Quit)
        self.act_quit.setMenuRole(QAction.MenuRole.QuitRole)
        self.act_undo = a("&Undo", self.undo, S.Undo)
        self.act_redo = a("&Redo", self.redo, S.Redo)
        self.act_copy = a("&Copy Selection", self.copy, S.Copy)
        self.act_paste = a("&Paste (click to place)", self.paste, S.Paste)
        self.act_paste_at = a("Paste at Coordinates…", self.paste_at, "Ctrl+Shift+V")
        self.act_fill = a("&Fill Selection with Pencil Colour", self.fill_selection, "Ctrl+Backspace")
        self.act_select_all = a("Select &All", self.select_all, S.SelectAll)
        self.act_deselect = a("&Deselect", self.deselect, "Ctrl+D")
        self.act_goto = a("&Go to Pixel…", self.goto_pixel, "Ctrl+G")
        self.act_transforms = {}
        for op, key in ((transforms.ROTATE_CW, "Ctrl+R"), (transforms.ROTATE_CCW, "Ctrl+Shift+R"),
                        (transforms.ROTATE_180, None), (transforms.FLIP_H, None), (transforms.FLIP_V, None)):
            self.act_transforms[op] = a(transforms.LABELS[op], lambda _=False, op=op: self.apply_transform(op), key)
        self.act_zoom_in = a("Zoom &In", lambda: self.canvas.zoom_in(), S.ZoomIn)
        self.act_zoom_out = a("Zoom &Out", lambda: self.canvas.zoom_out(), S.ZoomOut)
        self.act_zoom_100 = a("Actual Pixels (100%)", lambda: self.canvas.set_zoom(1), "Ctrl+0")
        self.act_zoom_fit = a("Fit in Window", lambda: self.canvas.zoom_to_fit(), "Ctrl+9")
        self.act_grid = a("Pixel &Grid", self.canvas.set_grid, "Ctrl+'", checkable=True,
                          tip=f"Show pixel boundaries at {zoom_label(4)} and above")
        self.act_highlight = a("&Highlight Changed Pixels", self._set_highlight, "Ctrl+Shift+H", checkable=True,
                               tip="Tint pixels whose values differ from the original (display only)")
        self.act_show_original = a("Show &Original (Before)", self._toggle_original, "\\", checkable=True,
                                   tip="Display the original buffer aligned to the current orientation")
        self.tool_group = QActionGroup(self)
        self.act_tools = {}
        for tool, label, key in (("inspect", "Inspect", "I"), ("pencil", "Pencil", "P"),
                                 ("select", "Rectangle Select", "M"), ("hand", "Hand (pan)", "H")):
            act = a(label, lambda _=False, t=tool: self.canvas.set_tool(t), key, checkable=True)
            self.tool_group.addAction(act)
            self.act_tools[tool] = act
        self.act_tools["inspect"].setChecked(True)
        self.act_about = a("About eXact", self.about)
        self.act_about.setMenuRole(QAction.MenuRole.AboutRole)
        self.act_guarantees = a("Preservation Guarantees", self.show_guarantees)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for act in (self.act_open, self.act_save, self.act_save_as):
            m.addAction(act)
        m.addSeparator()
        m.addAction(self.act_compare)
        m.addSeparator()
        comp = m.addMenu("PNG Compression Level")
        group = QActionGroup(self)
        current = self.compress_level
        for level, label in ((1, "1 — fastest"), (6, "6 — default"), (9, "9 — smallest file")):
            act = QAction(label, self, checkable=True)
            act.setChecked(level == current)
            act.triggered.connect(lambda _=False, lv=level: self.settings.setValue("compress_level", lv))
            group.addAction(act)
            comp.addAction(act)
        m.addSeparator()
        m.addAction(self.act_quit)

        m = mb.addMenu("&Edit")
        for act in (self.act_undo, self.act_redo, None, self.act_copy, self.act_paste, self.act_paste_at,
                    self.act_fill, None, self.act_select_all, self.act_deselect, None, self.act_goto):
            m.addSeparator() if act is None else m.addAction(act)

        m = mb.addMenu("&Image")
        for op in transforms.OPS:
            m.addAction(self.act_transforms[op])

        m = mb.addMenu("&View")
        for act in (self.act_zoom_in, self.act_zoom_out, self.act_zoom_100, self.act_zoom_fit):
            m.addAction(act)
        m.addSeparator()
        m.addAction(self.act_grid)
        m.addAction(self.act_show_original)
        m.addAction(self.act_highlight)
        m.addSeparator()
        for dock in self.docks.values():
            m.addAction(dock.toggleViewAction())

        m = mb.addMenu("&Tools")
        for act in self.act_tools.values():
            m.addAction(act)

        m = mb.addMenu("&Help")
        m.addAction(self.act_guarantees)
        m.addAction(self.act_about)

    def _build_toolbars(self) -> None:
        tb = self.addToolBar("Main")
        tb.setObjectName("main-toolbar")
        tb.addAction(self.act_open)
        tb.addAction(self.act_save)
        tb.addSeparator()
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)
        tb.addSeparator()
        for act in self.act_tools.values():
            tb.addAction(act)
        tb.addSeparator()
        self.zoom_combo = QComboBox()
        for z in ZOOM_LEVELS:
            self.zoom_combo.addItem(zoom_label(z), z)
        self.zoom_combo.setToolTip("Nearest-neighbour zoom (display only). Below 100% shows sampled pixels.")
        tb.addWidget(self.zoom_combo)
        self.display_combo = QComboBox()
        for key, label in DISPLAY_MODES.items():
            self.display_combo.addItem(label, key)
        self.display_combo.setToolTip("How raw channels are shown. Never changes stored pixels.")
        tb.addWidget(self.display_combo)
        self.compare_combo = QComboBox()
        for key, label in (("current", "Current"), ("original", "Original (before)"), ("split", "Split before | after")):
            self.compare_combo.addItem(label, key)
        tb.addWidget(self.compare_combo)
        self.split_slider = QSlider(Qt.Orientation.Horizontal)
        self.split_slider.setRange(0, 1000)
        self.split_slider.setValue(500)
        self.split_slider.setMaximumWidth(140)
        self.split_slider.setToolTip("Split position: original on the left, current on the right")
        tb.addWidget(self.split_slider)

        self.addToolBarBreak()
        tb2 = self.addToolBar("Tool options")
        tb2.setObjectName("tool-options")
        self.tool_options = ToolOptions(self.tool_settings)
        tb2.addWidget(self.tool_options)
        tb2.addSeparator()
        for op in transforms.OPS:
            tb2.addAction(self.act_transforms[op])

    def _build_statusbar(self) -> None:
        self.pos_label = QLabel()
        self.zoom_status = QLabel()
        self.statusBar().addPermanentWidget(self.pos_label)
        self.statusBar().addPermanentWidget(self.zoom_status)

    def _connect(self) -> None:
        c = self.canvas
        c.zoomChanged.connect(self._zoom_changed)
        c.hoverChanged.connect(self._hover_changed)
        c.pixelPicked.connect(self.set_inspected)
        c.strokeFinished.connect(self._stroke_finished)
        c.colorPicked.connect(self.tool_options.set_color)
        c.selectionChanged.connect(lambda _: self._update_actions())
        c.pasteCommitted.connect(self._paste_commit)
        c.pasteCancelled.connect(lambda: self.statusBar().showMessage("Paste cancelled", 3000))
        self.inspector.applyRequested.connect(self._apply_pixel)
        self.inspector.useAsColor.connect(self.tool_options.set_color)
        self.hex_panel.offsetChosen.connect(self._goto_offset)
        self.integrity_panel.recomputeRequested.connect(self._start_hashing)
        self.zoom_combo.activated.connect(lambda i: self.canvas.set_zoom(self.zoom_combo.itemData(i)))
        self.display_combo.currentIndexChanged.connect(self._display_mode_changed)
        self.compare_combo.currentIndexChanged.connect(self._compare_changed)
        self.split_slider.valueChanged.connect(self._split_changed)
        self._zoom_changed(self.canvas.zoom)

    @property
    def compress_level(self) -> int:
        try:
            return int(self.settings.value("compress_level", 6))
        except (TypeError, ValueError):
            return 6

    def set_busy(self, busy: bool) -> None:
        self.canvas.busy = busy

    def set_document(self, doc: Document | None) -> None:
        self.doc = doc
        self.hashes = None
        self.canvas.set_document(doc)
        self.tool_options.set_mode(doc.mode if doc else None)
        if doc is not None:
            self._display_mode_changed()
            self._compare_changed()
            self.canvas.doc_source.highlight_changes = self.act_highlight.isChecked()
            self.canvas.zoom_to_fit()
        self.inspector.show_pixel(None, None)
        self.hex_panel.show_bytes(None, None)
        self.info_panel.show_document(doc)
        self.document_changed(None)
        if doc is not None:
            self.set_inspected(0, 0)

    def document_changed(self, summary: OperationSummary | None, *, region: Rect | None = None) -> None:
        doc = self.doc
        if summary is not None:
            self.statusBar().showMessage(summary.describe(), 15000)
        if region is not None:
            self.canvas.update_image_rect(region)
        else:
            self.canvas.viewport().update()
        if doc is not None:
            self.hashes = None
            self.hash_timer.start()
            if self.canvas.inspected is not None:
                x, y = self.canvas.inspected
                if x < doc.width and y < doc.height:
                    self.set_inspected(x, y)
                else:
                    self.set_inspected(0, 0)
        self.integrity_panel.show_document(doc, self.hashes)
        self._update_title()
        self._update_actions()

    def _update_title(self) -> None:
        doc = self.doc
        name = short_path(doc.path) if doc else "No image"
        self.setWindowTitle(f"{name}[*] — eXact {__version__}")
        self.setWindowModified(bool(doc and doc.dirty))

    def _update_actions(self) -> None:
        has = self.doc is not None
        for act in (self.act_save, self.act_save_as, self.act_select_all, self.act_goto, self.act_zoom_in,
                    self.act_zoom_out, self.act_zoom_100, self.act_zoom_fit, *self.act_transforms.values()):
            act.setEnabled(has)
        has_sel = has and self.canvas.selection is not None
        self.act_copy.setEnabled(has_sel)
        self.act_fill.setEnabled(has_sel)
        self.act_deselect.setEnabled(has_sel)
        self.act_paste.setEnabled(has and self.clipboard is not None)
        self.act_paste_at.setEnabled(has and self.clipboard is not None)
        h = self.doc.history if has else None
        self.act_undo.setEnabled(bool(h and h.can_undo))
        self.act_redo.setEnabled(bool(h and h.can_redo))
        self.act_undo.setText(f"&Undo {h.undo_label}" if h and h.can_undo else "&Undo")
        self.act_redo.setText(f"&Redo {h.redo_label}" if h and h.can_redo else "&Redo")

    def _start_hashing(self) -> None:
        if self.doc is None:
            return
        if not self.worker.start(self.doc):
            self.hash_timer.start()

    def _hashes_ready(self, doc, generation: int, hashes: dict) -> None:
        if doc is not self.doc:
            return
        if "error" in hashes or generation != doc.generation:
            self.hash_timer.start()
            return
        self.hashes = hashes
        self.integrity_panel.show_document(doc, hashes)

    def _zoom_changed(self, zoom: float) -> None:
        self.zoom_status.setText(f"Zoom {zoom_label(zoom)}")
        idx = self.zoom_combo.findData(zoom)
        if idx >= 0:
            self.zoom_combo.setCurrentIndex(idx)

    def _hover_changed(self, pixel) -> None:
        if pixel is None or self.doc is None:
            self.pos_label.setText("")
            return
        x, y = pixel
        values = self.doc.get_pixel(x, y)
        hexed = "".join(f"{v:02X}" for v in values)
        self.pos_label.setText(f"({x}, {y})  {' '.join(map(str, values))}  #{hexed}")
        if self.hex_panel.follow.isChecked():
            self.hex_panel.show_bytes(self.doc, pixel_offset(x, y, self.doc.width, self.doc.channels), self.doc.channels)

    def _display_mode_changed(self) -> None:
        if self.canvas.doc_source:
            self.canvas.doc_source.display_mode = self.display_combo.currentData()
            self.canvas.viewport().update()

    def _compare_changed(self) -> None:
        mode = self.compare_combo.currentData()
        self.split_slider.setEnabled(mode == "split")
        self.act_show_original.blockSignals(True)
        self.act_show_original.setChecked(mode == "original")
        self.act_show_original.blockSignals(False)
        if self.canvas.doc_source:
            self.canvas.doc_source.compare = mode
            self.canvas.viewport().update()
        if mode == "original":
            self.statusBar().showMessage("Showing ORIGINAL pixels (aligned to current orientation). Edits still apply to the current buffer.")

    def _toggle_original(self, on: bool) -> None:
        self.compare_combo.setCurrentIndex(self.compare_combo.findData("original" if on else "current"))

    def _split_changed(self, value: int) -> None:
        if self.canvas.doc_source:
            self.canvas.doc_source.split = value / 1000
            self.canvas.viewport().update()

    def _set_highlight(self, on: bool) -> None:
        if self.canvas.doc_source:
            self.canvas.doc_source.highlight_changes = on
            self.canvas.viewport().update()

    def set_inspected(self, x: int, y: int) -> None:
        if self.doc is None:
            return
        self.canvas.set_inspected((x, y))
        self.inspector.show_pixel(self.doc, (x, y))
        if not self.hex_panel.follow.isChecked():
            self.hex_panel.show_bytes(self.doc, pixel_offset(x, y, self.doc.width, self.doc.channels), self.doc.channels)

    def _goto_offset(self, offset: int) -> None:
        doc = self.doc
        if doc is None or offset >= doc.pixels.size:
            self.statusBar().showMessage("Offset outside the pixel buffer", 4000)
            return
        index = offset // doc.channels
        x, y = index % doc.width, index // doc.width
        self.set_inspected(x, y)
        self.canvas.center_on(x + 0.5, y + 0.5)

    def _stroke_finished(self, summary: OperationSummary) -> None:
        self.document_changed(summary)

    def _apply_pixel(self, x: int, y: int, values) -> None:
        if self.doc is None:
            return
        summary = self.doc.set_pixel(x, y, values)
        self.document_changed(summary, region=Rect(x, y, 1, 1))

    def open_dialog(self) -> None:
        if not self.maybe_save_changes():
            return
        start = os.path.dirname(self.doc.path) if self.doc and self.doc.path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open PNG", start, "PNG images (*.png);;All files (*)")
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> bool:
        name = os.path.basename(path)
        try:
            doc = run_blocking(self, f"Opening {name}…", Document.open, path)
        except UnsupportedPngError as exc:
            QMessageBox.warning(self, "Unsupported PNG",
                                f"<b>{name}</b> was not opened.<br><br>{exc}<br><br>No conversion was performed.")
            return False
        except PngFormatError as exc:
            QMessageBox.warning(self, "Invalid PNG", f"{name} is not a valid PNG: {exc}")
            return False
        except OSError as exc:
            QMessageBox.warning(self, "Cannot open", str(exc))
            return False
        self.set_document(doc)
        serious = [w for w in doc.warnings if any(k in w for k in ("APNG", "CRC", "after IEND"))]
        if serious:
            QMessageBox.warning(self, "Opened with warnings", "\n\n".join(serious))
        self.statusBar().showMessage(
            f"Opened {name}: {doc.width}×{doc.height} {doc.mode}, pixel SHA-256 {doc.original_sha256[:16]}…"
            + (f"  ({len(doc.warnings)} note(s) in Image Info)" if doc.warnings else ""), 15000)
        return True

    def maybe_save_changes(self) -> bool:
        if not (self.doc and self.doc.dirty):
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", f"Save changes to {short_path(self.doc.path)}?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save() is not None
        return answer == QMessageBox.StandardButton.Discard

    def save(self):
        if self.doc is None:
            return None
        if not self.doc.path:
            return self.save_as()
        return self.save_to(self.doc.path)

    def save_as(self):
        if self.doc is None:
            return None
        path, _ = QFileDialog.getSaveFileName(self, "Save PNG As", self.doc.path or "untitled.png",
                                              "PNG images (*.png)")
        if not path:
            return None
        if not path.lower().endswith(".png"):
            path += ".png"
        return self.save_to(path)

    def save_to(self, path: str, *, interactive: bool = True):
        doc = self.doc
        if doc is None:
            return None
        same_file = bool(doc.path) and os.path.realpath(path) == os.path.realpath(doc.path)
        if same_file and not doc.dirty and os.path.exists(path):
            self.statusBar().showMessage(
                "Nothing to save: pixel buffer is unchanged since it was loaded/saved. File left untouched.", 8000)
            return None
        if same_file and interactive and doc.disk_changed_since_load_or_save():
            answer = QMessageBox.warning(
                self, "File changed on disk",
                f"{short_path(path)} was modified by another program since eXact opened or saved it.\n\n"
                "Overwrite it with the current buffer?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return None
        plan = doc.plan_save(path)
        if plan.lossy_metadata and interactive:
            listing = "\n".join(f"• {d.name}: {d.reason}" for d in plan.dropped)
            answer = QMessageBox.warning(
                self, "Some file data cannot be preserved",
                "Every pixel byte will be preserved and verified, but the following PNG data "
                f"cannot be carried over:\n\n{listing}\n\nSave anyway?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Save:
                return None
        try:
            report = run_blocking(self, "Encoding, re-decoding and verifying…", doc.save, plan,
                                  compress_level=self.compress_level)
        except SaveVerificationError as exc:
            QMessageBox.critical(self, "Save verification FAILED",
                                 f"The written file did not decode to the intended pixels:\n\n{exc}\n\n"
                                 f"{short_path(path)} was NOT modified.")
            return None
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", f"{exc}\n\n{short_path(path)} was NOT modified.")
            return None
        changed = [d for d in report.decisions if d.action in ("updated", "dropped")]
        kept = sum(1 for d in report.decisions if d.action == "preserved")
        note = (f"saved and verified {short_path(report.dest)} — decoded pixels identical to the buffer "
                f"(SHA-256 {report.pixel_sha256[:16]}…); {kept} ancillary chunk(s) preserved"
                + (f", {len(changed)} updated/dropped" if changed else "")
                + f"; file {report.file_size:,} bytes, {report.elapsed:.1f}s")
        doc.last_operation = OperationSummary("Save", 0, 0, None, note=note)
        self.info_panel.show_document(doc)
        self.document_changed(doc.last_operation)
        return report

    def compare_dialog(self) -> None:
        DiffDialog(self, self.doc).exec()

    def _confirm_transform(self, op: str) -> bool:
        if self.settings.value("skip_transform_notice", False, type=bool):
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(transforms.LABELS[op])
        box.setText("This transform moves pixels without changing any channel value.")
        box.setInformativeText(
            "However, the order of bytes in the pixel buffer changes. Data stored in pixel order "
            "(such as an ImageStore payload) will NOT decode until the inverse transform is applied.\n\n"
            "Continue?")
        dont_ask = QCheckBox("Don't show this again")
        box.setCheckBox(dont_ask)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return False
        if dont_ask.isChecked():
            self.settings.setValue("skip_transform_notice", True)
        return True

    def apply_transform(self, op: str, *, confirm: bool = True) -> OperationSummary | None:
        if self.doc is None:
            return None
        if confirm and not self._confirm_transform(op):
            return None
        self.canvas.set_selection(None)
        summary = self._maybe_blocking(transforms.LABELS[op] + "…", self.doc.transform, op)
        self.document_changed(summary)
        return summary

    def _maybe_blocking(self, label: str, fn, *args):
        if self.doc.width * self.doc.height >= HEAVY_PIXELS:
            return run_blocking(self, label, fn, *args)
        return fn(*args)

    def undo(self) -> None:
        if self.doc is None:
            return
        heavy = isinstance(self.doc.history.peek_undo(), TransformCommand)
        summary = self._maybe_blocking("Undoing…", self.doc.undo) if heavy else self.doc.undo()
        if summary is not None:
            if heavy:
                self.canvas.set_selection(None)
            self.document_changed(summary)

    def redo(self) -> None:
        if self.doc is None:
            return
        heavy = isinstance(self.doc.history.peek_redo(), TransformCommand)
        summary = self._maybe_blocking("Redoing…", self.doc.redo) if heavy else self.doc.redo()
        if summary is not None:
            if heavy:
                self.canvas.set_selection(None)
            self.document_changed(summary)

    def select_all(self) -> None:
        if self.doc:
            self.canvas.set_selection(Rect(0, 0, self.doc.width, self.doc.height))

    def deselect(self) -> None:
        self.canvas.set_selection(None)

    def copy(self) -> None:
        if self.doc is None or self.canvas.selection is None:
            return
        self.clipboard = self.doc.copy_rect(self.canvas.selection)
        self.statusBar().showMessage(
            f"Copied {self.clipboard.width}×{self.clipboard.height} {self.clipboard.mode} pixels "
            "(internal clipboard, raw channel values)", 6000)
        self._update_actions()

    def _confirm_channel_mismatch(self) -> bool:
        note = self.doc.paste_channel_note(self.clipboard)
        if note is None:
            return True
        answer = QMessageBox.warning(self, "Channel mismatch", note + "\n\nPaste anyway?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def paste(self) -> None:
        if self.doc is None or self.clipboard is None or not self._confirm_channel_mismatch():
            return
        self.canvas.begin_paste(self.clipboard)
        self.statusBar().showMessage("Click to place the top-left corner of the paste. Esc or right-click cancels.")

    def paste_at(self) -> None:
        if self.doc is None or self.clipboard is None:
            return
        default = (self.canvas.selection.x, self.canvas.selection.y) if self.canvas.selection else (0, 0)
        xy = self._ask_xy("Paste at", default)
        if xy is not None and self._confirm_channel_mismatch():
            self._paste_commit(*xy)

    def _paste_commit(self, x: int, y: int) -> None:
        self.canvas.finish_paste()
        try:
            summary = self.doc.paste(self.clipboard, x, y, allow_channel_mismatch=True)
        except (ValueError, ChannelMismatchError) as exc:
            QMessageBox.warning(self, "Paste", str(exc))
            return
        self.canvas.set_selection(Rect(x, y, self.clipboard.width, self.clipboard.height).clamp(
            self.doc.width, self.doc.height))
        self.document_changed(summary)

    def fill_selection(self) -> None:
        if self.doc is None or self.canvas.selection is None:
            return
        ts = self.tool_settings
        summary = self.doc.fill_rect(self.canvas.selection, ts.color, ts.mask)
        self.document_changed(summary, region=self.canvas.selection)

    def _ask_xy(self, title: str, default: tuple[int, int]) -> tuple[int, int] | None:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        form = QFormLayout(dialog)
        sx, sy = QSpinBox(), QSpinBox()
        sx.setRange(-self.doc.width, self.doc.width - 1)
        sy.setRange(-self.doc.height, self.doc.height - 1)
        sx.setValue(default[0])
        sy.setValue(default[1])
        form.addRow("x:", sx)
        form.addRow("y:", sy)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return sx.value(), sy.value()

    def goto_pixel(self) -> None:
        if self.doc is None:
            return
        current = self.canvas.inspected or (0, 0)
        xy = self._ask_xy("Go to pixel", current)
        if xy is None:
            return
        x = min(max(xy[0], 0), self.doc.width - 1)
        y = min(max(xy[1], 0), self.doc.height - 1)
        if self.canvas.zoom < 8:
            self.canvas.set_zoom(16)
        self.canvas.center_on(x + 0.5, y + 0.5)
        self.set_inspected(x, y)

    def about(self) -> None:
        QMessageBox.about(
            self, "About eXact",
            f"<h2>eXact {__version__}</h2><p><b>eXtensible Accurate Color Toolkit</b></p>"
            f"<p><i>{TAGLINE}</i></p><p>A binary-preserving pixel editor for 8-bit RGB/RGBA PNGs.</p>"
            f"<p>{MOTTO}</p>")

    def show_guarantees(self) -> None:
        QMessageBox.information(self, "Preservation guarantees", GUARANTEES_TEXT)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if urls and self.maybe_save_changes():
            self.open_path(urls[0].toLocalFile())

    def closeEvent(self, event) -> None:
        if self.maybe_save_changes():
            event.accept()
        else:
            event.ignore()
