"""Main editor window: a canvas-first workspace.

    top     menu bar and one compact toolbar (file, history, zoom, view, workspace)
    left    tool palette (inspect, pencil, eyedropper, select, hand, zoom, transforms)
    centre  the canvas
    right   Tool properties, Pixel inspector, History
    bottom  Hex bytes, Integrity details, Image info (collapsible, tabbed)

Two workspace presets (Edit, Inspect) arrange the docks; each remembers changes the user
makes to it. Integrity is always visible in the status bar.
"""

from __future__ import annotations

import os
import threading
import time

from PyQt6.QtCore import QObject, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolBar,
    QToolButton,
    QWidget,
)

from .. import MOTTO, TAGLINE, __version__
from ..core import transforms
from ..core.document import ChannelMismatchError, Clip, Document, OperationSummary, Rect
from ..core.history import TransformCommand
from ..core.pixels import pixel_offset
from . import style
from .canvas import ZOOM_LEVELS, EditorCanvas, zoom_label
from .diff_dialog import DiffDialog
from .errors import show_error
from .hexview import HexPanel
from .panels import (
    HistoryPanel,
    InfoPanel,
    InspectorPanel,
    IntegrityPanel,
    ToolPanel,
    ToolSettings,
    integrity_state,
    restyle_chips,
    short_path,
    swatch,
)
from .tasks import TaskCancelled, run_blocking

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
<li>The canvas, hex view and inspector only display the buffer; what is saved always comes from
    the authoritative pixel buffer, never from rendered tiles.</li>
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

TOOLS = (
    ("inspect", "Inspect", "inspect", "I"),
    ("pencil", "Pencil", "pencil", "P"),
    ("eyedropper", "Eyedropper", "eyedropper", "E"),
    ("select", "Rectangle Select", "select", "M"),
    ("hand", "Hand", "hand", "H"),
    ("zoom", "Zoom", "zoom", "Z"),
)
DISPLAY_CHOICES = (
    ("composite", "Composite", "Composite over a checkerboard (alpha applied for display only)"),
    ("rgb", "RGB, ignore alpha", "Raw RGB with alpha ignored: reveals data hidden under transparent pixels"),
    ("alpha", "Alpha", "The alpha channel as greyscale"),
    ("r", "Red", "The R channel as greyscale"),
    ("g", "Green", "The G channel as greyscale"),
    ("b", "Blue", "The B channel as greyscale"),
)
COMPARE_CHOICES = (("current", "Current", "eye"), ("original", "Original", "history"), ("split", "Split", "split"))
DOCKS = (
    ("tool", "Tool", Qt.DockWidgetArea.RightDockWidgetArea),
    ("inspector", "Pixel", Qt.DockWidgetArea.RightDockWidgetArea),
    ("history", "History", Qt.DockWidgetArea.RightDockWidgetArea),
    ("hex", "Hex", Qt.DockWidgetArea.BottomDockWidgetArea),
    ("integrity", "Integrity", Qt.DockWidgetArea.BottomDockWidgetArea),
    ("info", "Image Info", Qt.DockWidgetArea.BottomDockWidgetArea),
)
WORKSPACES = {
    "edit": ("Edit", "pencil", {"tool", "inspector", "history"}),
    "inspect": ("Inspect", "verify", {"inspector", "hex", "integrity", "info"}),
}


class IntegrityWorker(QObject):
    """Hashes the buffer on a background thread. The GUI debounces requests so a full-image
    SHA-256 never runs per brush movement; results for a stale generation are discarded."""

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
            try:
                self.done.emit(doc, generation, hashes)
            except RuntimeError:  # the window closed while hashing: nobody to tell
                pass

        threading.Thread(target=work, name="exact-integrity", daemon=True).start()
        return True


class Segmented(QFrame):
    """Exclusive checkable buttons in one frame (view mode, workspace)."""

    changed = pyqtSignal(str)

    def __init__(self, items, *, text: bool = True, parent=None):
        super().__init__(parent)
        self.setProperty("role", "segmented")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QToolButton] = {}
        for key, label, icon, *tip in items:
            b = QToolButton()
            b.setIcon(style.icon(icon))
            b.setText(label)
            b.setToolTip(tip[0] if tip else label)
            b.setAccessibleName(label)
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if text
                                 else Qt.ToolButtonStyle.ToolButtonIconOnly)
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)
            b.toggled.connect(lambda on, k=key: on and self.changed.emit(k))
        next(iter(self.buttons.values())).setChecked(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def value(self) -> str:
        return next(k for k, b in self.buttons.items() if b.isChecked())

    def set_value(self, key: str, *, emit: bool = True) -> None:
        if not emit:
            self.blockSignals(True)
            for b in self.buttons.values():
                b.blockSignals(True)
        self.buttons[key].setChecked(True)
        if not emit:
            for b in self.buttons.values():
                b.blockSignals(False)
            self.blockSignals(False)

    # QComboBox-like access, used by tests and older call sites
    def count(self) -> int:
        return len(self.buttons)

    def setCurrentIndex(self, i: int) -> None:  # noqa: N802
        list(self.buttons.values())[i].setChecked(True)

    def findData(self, key: str) -> int:  # noqa: N802
        return list(self.buttons).index(key) if key in self.buttons else -1

    def currentData(self) -> str:  # noqa: N802
        return self.value()


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self.settings = settings or QSettings("eXact", "eXact")
        self.doc: Document | None = None
        self.clipboard: Clip | None = None
        self.hashes: dict | None = None
        self.hashed_at: float | None = None
        self.workspace: str | None = None
        self.tool_settings = ToolSettings()
        self.setWindowIcon(style.app_icon("logo-exact"))
        self.resize(1440, 900)
        self.setAcceptDrops(True)
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks | QMainWindow.DockOption.AllowTabbedDocks
                            | QMainWindow.DockOption.AllowNestedDocks)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)

        self.canvas = EditorCanvas(self.tool_settings)
        self.canvas.setFrameShape(QFrame.Shape.NoFrame)
        self.setCentralWidget(self.canvas)
        self.tool_panel = ToolPanel(self.tool_settings)
        self.tool_options = self.tool_panel  # older name
        self.inspector = InspectorPanel()
        self.history_panel = HistoryPanel()
        self.hex_panel = HexPanel()
        self.integrity_panel = IntegrityPanel()
        self.info_panel = InfoPanel()
        widgets = {"tool": self.tool_panel, "inspector": self.inspector, "history": self.history_panel,
                   "hex": self.hex_panel, "integrity": self.integrity_panel, "info": self.info_panel}
        self.docks: dict[str, QDockWidget] = {}
        for key, title, area in DOCKS:
            dock = QDockWidget(title, self)
            dock.setObjectName(key)
            widget = widgets[key]
            if key in ("tool", "inspector", "integrity"):
                scroller = QScrollArea()
                scroller.setWidgetResizable(True)
                scroller.setFrameShape(QFrame.Shape.NoFrame)
                scroller.setWidget(widget)
                widget = scroller
            dock.setWidget(widget)
            self.addDockWidget(area, dock)
            self.docks[key] = dock
        self.tabifyDockWidget(self.docks["hex"], self.docks["integrity"])
        self.tabifyDockWidget(self.docks["integrity"], self.docks["info"])
        self.docks["hex"].raise_()
        self.resizeDocks([self.docks["tool"]], [340], Qt.Orientation.Horizontal)
        self.resizeDocks([self.docks["hex"]], [250], Qt.Orientation.Vertical)

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
        self.set_workspace(str(self.settings.value("workspace", "edit")), save_current=False)
        self.set_document(None)

    # ---------------------------------------------------------------- construction

    def _action(self, text, slot=None, shortcut=None, checkable=False, tip=None, icon=None,
                toggle: bool = False) -> QAction:
        """toggle=True connects *slot* to toggled(bool), so it also runs when the state is set
        from code (setChecked); otherwise to triggered (user activation only)."""
        action = QAction(text, self)
        if icon:
            action.setIcon(style.icon(icon))
        if shortcut is not None:
            if isinstance(shortcut, (list, tuple)):
                action.setShortcuts([QKeySequence(s) for s in shortcut])
            else:
                action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(checkable)
        if tip:
            action.setStatusTip(tip)
        keys = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        action.setToolTip(f"{(tip or text).replace('&', '')}" + (f"  ({keys})" if keys else ""))
        if slot is not None:
            (action.toggled if toggle else action.triggered).connect(slot)
        return action

    def _build_actions(self) -> None:
        a = self._action
        S = QKeySequence.StandardKey
        self.act_open = a("&Open…", self.open_dialog, S.Open, icon="open")
        self.act_save = a("&Save", self.save, S.Save, icon="save",
                          tip="Save: encode, verify by re-decoding, then atomically replace")
        self.act_save_as = a("Save &As…", self.save_as, S.SaveAs)
        self.act_compare = a("Compare Pixel Buffers…", self.compare_dialog, "Ctrl+Shift+D", icon="compare",
                             tip="Compare the decoded pixels of two PNGs, or of this buffer and a PNG")
        self.act_compare_disk = a("Compare with File on Disk", self.compare_with_disk,
                                  tip="Compare the current buffer with the saved file it came from")
        self.act_quit = a("&Quit", self.close, S.Quit)
        self.act_quit.setMenuRole(QAction.MenuRole.QuitRole)
        self.act_undo = a("&Undo", self.undo, S.Undo, icon="undo")
        self.act_redo = a("&Redo", self.redo, [QKeySequence(S.Redo).toString(), "Ctrl+Shift+Z"], icon="redo")
        self.act_copy = a("&Copy Selection", self.copy, S.Copy, icon="copy")
        self.act_paste = a("&Paste (click to place)", self.paste, S.Paste, icon="paste")
        self.act_paste_at = a("Paste at Coordinates…", self.paste_at, "Ctrl+Shift+V")
        self.act_fill = a("&Fill Selection with Pencil Colour", self.fill_selection, "Ctrl+Backspace", icon="fill")
        self.act_select_all = a("Select &All", self.select_all, S.SelectAll)
        self.act_deselect = a("&Deselect", self.deselect, "Ctrl+D")
        self.act_goto = a("&Go to Pixel…", self.goto_pixel, "Ctrl+G", icon="goto")
        self.act_restore_pixel = a("Restore Inspected Pixel to Original", self.restore_inspected_pixel,
                                   tip="Write the inspected pixel's original values back (undoable)")
        self.act_transforms = {}
        icons = {transforms.ROTATE_CW: "rotate-cw", transforms.ROTATE_CCW: "rotate-ccw",
                 transforms.ROTATE_180: "rotate-180", transforms.FLIP_H: "flip-h", transforms.FLIP_V: "flip-v"}
        for op, key in ((transforms.ROTATE_CW, "Ctrl+R"), (transforms.ROTATE_CCW, "Ctrl+Shift+R"),
                        (transforms.ROTATE_180, None), (transforms.FLIP_H, None), (transforms.FLIP_V, None)):
            self.act_transforms[op] = a(transforms.LABELS[op], lambda _=False, op=op: self.apply_transform(op), key,
                                        icon=icons[op])
        self.act_zoom_in = a("Zoom &In", lambda: self.canvas.zoom_in(), [S.ZoomIn, "Ctrl+="], icon="zoom-in")
        self.act_zoom_out = a("Zoom &Out", lambda: self.canvas.zoom_out(), S.ZoomOut, icon="zoom-out")
        self.act_zoom_100 = a("Actual Pixels (100%)", lambda: self.canvas.set_zoom(1), "Ctrl+0", icon="zoom-100")
        self.act_zoom_fit = a("Fit in Window", lambda: self.canvas.zoom_to_fit(), "Ctrl+9", icon="zoom-fit")
        self.act_grid = a("Pixel &Grid", self.canvas.set_grid, "Ctrl+'", checkable=True, icon="grid", toggle=True,
                          tip=f"Pixel grid (shown at {zoom_label(4)} and above)")
        self.act_highlight = a("&Highlight Changed Pixels", self._set_highlight, "Ctrl+Shift+H", checkable=True,
                               toggle=True,
                               icon="highlight", tip="Tint pixels whose values differ from the original (display only)")
        self.act_show_original = a("Show &Original (Before)", self._toggle_original, "\\", checkable=True,
                                   toggle=True,
                                   tip="Display the original buffer aligned to the current orientation")
        self.tool_group = QActionGroup(self)
        self.act_tools = {}
        for tool, label, icon, key in TOOLS:
            act = a(label, lambda _=False, t=tool: self.set_tool(t), key, checkable=True, icon=icon)
            self.tool_group.addAction(act)
            self.act_tools[tool] = act
        self.act_tools["inspect"].setChecked(True)
        self.act_size_down = a("Smaller Pencil", lambda: self._nudge_size(-1), "[")
        self.act_size_up = a("Larger Pencil", lambda: self._nudge_size(1), "]")
        self.act_workspaces = {}
        ws_group = QActionGroup(self)
        for i, (key, (label, icon, _)) in enumerate(WORKSPACES.items(), 1):
            act = a(f"{label} Workspace", lambda _=False, k=key: self.set_workspace(k), f"Ctrl+{i}",
                    checkable=True, icon=icon)
            ws_group.addAction(act)
            self.act_workspaces[key] = act
        self.act_reset_workspace = a("Reset Workspace Layout", self.reset_workspace)
        self.act_about = a("About eXact", self.about)
        self.act_about.setMenuRole(QAction.MenuRole.AboutRole)
        self.act_guarantees = a("Preservation Guarantees", self.show_guarantees, icon="shield")
        self.act_shortcuts = a("Keyboard Shortcuts", self.show_shortcuts, "F1")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for act in (self.act_open, self.act_save, self.act_save_as):
            m.addAction(act)
        m.addSeparator()
        m.addAction(self.act_compare)
        m.addAction(self.act_compare_disk)
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
                    self.act_fill, None, self.act_select_all, self.act_deselect, None, self.act_goto,
                    self.act_restore_pixel):
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
        for act in self.act_workspaces.values():
            m.addAction(act)
        m.addAction(self.act_reset_workspace)
        panels = m.addMenu("Panels")
        for dock in self.docks.values():
            panels.addAction(dock.toggleViewAction())
        theme = m.addMenu("Theme")
        group = QActionGroup(self)
        for key, label in (("dark", "Dark"), ("light", "Light")):
            act = QAction(label, self, checkable=True)
            act.setChecked(style.theme_name() == key)
            act.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            group.addAction(act)
            theme.addAction(act)

        m = mb.addMenu("&Tools")
        for act in self.act_tools.values():
            m.addAction(act)
        m.addSeparator()
        m.addAction(self.act_size_down)
        m.addAction(self.act_size_up)

        m = mb.addMenu("&Help")
        m.addAction(self.act_shortcuts)
        m.addAction(self.act_guarantees)
        m.addAction(self.act_about)

    def _build_toolbars(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("main-toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        for act in (self.act_open, self.act_save, None, self.act_undo, self.act_redo, None,
                    self.act_zoom_out):
            tb.addSeparator() if act is None else tb.addAction(act)
        self.zoom_combo = QComboBox()
        for z in ZOOM_LEVELS:
            self.zoom_combo.addItem(zoom_label(z), z)
        self.zoom_combo.setToolTip("Nearest-neighbour zoom (display only). Below 100% shows sampled pixels.")
        self.zoom_combo.setAccessibleName("Zoom level")
        self.zoom_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        tb.addWidget(self.zoom_combo)
        for act in (self.act_zoom_in, self.act_zoom_fit, self.act_zoom_100, None, self.act_grid, self.act_highlight):
            tb.addSeparator() if act is None else tb.addAction(act)
        tb.addSeparator()
        self.display_combo = QComboBox()
        for i, (key, label, tip) in enumerate(DISPLAY_CHOICES):
            self.display_combo.addItem(label, key)
            self.display_combo.setItemData(i, tip, Qt.ItemDataRole.ToolTipRole)
        self.display_combo.setToolTip("How raw channels are shown. Never changes stored pixels.")
        self.display_combo.setAccessibleName("Display mode")
        tb.addWidget(self.display_combo)
        tb.addSeparator()
        self.compare_combo = Segmented([(k, label, icon, tip) for k, label, icon, tip in (
            ("current", "Current", "eye", "Show the current buffer"),
            ("original", "Original", "history", "Show the original buffer, aligned to the current orientation (\\)"),
            ("split", "Split", "split", "Original on the left, current on the right; drag the slider to move the split"),
        )])
        tb.addWidget(self.compare_combo)
        self.split_slider = QSlider(Qt.Orientation.Horizontal)
        self.split_slider.setRange(0, 1000)
        self.split_slider.setValue(500)
        self.split_slider.setFixedWidth(110)
        self.split_slider.setToolTip("Split position: original on the left, current on the right")
        self.split_slider.setAccessibleName("Split position")
        self.split_action = tb.addWidget(self.split_slider)
        tb.addSeparator()
        tb.addAction(self.act_compare)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.workspace_switch = Segmented([(k, label, icon, f"{label} workspace (Ctrl+{i})")
                                           for i, (k, (label, icon, _)) in enumerate(WORKSPACES.items(), 1)])
        tb.addWidget(self.workspace_switch)

        pal = QToolBar("Tools")
        pal.setObjectName("ToolPalette")
        pal.setMovable(False)
        pal.setOrientation(Qt.Orientation.Vertical)
        pal.setIconSize(QSize(20, 20))
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, pal)
        for act in self.act_tools.values():
            pal.addAction(act)
        pal.addSeparator()
        for op in (transforms.ROTATE_CCW, transforms.ROTATE_CW, transforms.FLIP_H, transforms.FLIP_V):
            pal.addAction(self.act_transforms[op])
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        pal.addWidget(spacer)
        self.color_chip = QToolButton()
        self.color_chip.setIconSize(QSize(28, 14))
        self.color_chip.setAccessibleName("Pencil colour")
        self.color_chip.clicked.connect(lambda: (self.set_tool("pencil"), self.docks["tool"].show(),
                                                 self.docks["tool"].raise_()))
        pal.addWidget(self.color_chip)
        self.palette = pal

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(True)
        self.integrity_chip = QLabel()
        self.integrity_chip.setToolTip("Integrity versus the original buffer")
        self.save_chip = QLabel()
        self.pos_label = QLabel()
        self.pos_label.setFont(style.mono_font(0.95))
        self.pos_label.setMinimumWidth(320)
        self.doc_label = QLabel()
        self.zoom_status = QLabel()
        for w in (self.pos_label, self.doc_label, self.zoom_status, self.integrity_chip, self.save_chip):
            sb.addPermanentWidget(w)

    def _connect(self) -> None:
        c = self.canvas
        c.zoomChanged.connect(self._zoom_changed)
        c.hoverChanged.connect(self._hover_changed)
        c.pixelPicked.connect(self.set_inspected)
        c.strokeFinished.connect(self._stroke_finished)
        c.colorPicked.connect(self._color_picked)
        c.selectionChanged.connect(lambda _: self._selection_changed())
        c.pasteCommitted.connect(self._paste_commit)
        c.pasteCancelled.connect(lambda: self.statusBar().showMessage("Paste cancelled", 3000))
        self.inspector.applyRequested.connect(self._apply_pixel)
        self.inspector.useAsColor.connect(self.tool_panel.set_color)
        self.inspector.gotoRequested.connect(self.goto_pixel)
        self.tool_panel.changed.connect(self._tool_settings_changed)
        self.tool_panel.selectionEdited.connect(self._selection_edited)
        self.tool_panel.actionRequested.connect(self._tool_action)
        self.history_panel.jumpRequested.connect(self.jump_to_history)
        self.history_panel.undoRequested.connect(self.undo)
        self.history_panel.redoRequested.connect(self.redo)
        self.hex_panel.offsetChosen.connect(self._goto_offset)
        self.hex_panel.pixelChosen.connect(self._goto_xy)
        self.integrity_panel.recomputeRequested.connect(self._start_hashing)
        self.integrity_panel.compareRequested.connect(self.compare_dialog)
        self.zoom_combo.activated.connect(lambda i: self.canvas.set_zoom(self.zoom_combo.itemData(i)))
        self.display_combo.currentIndexChanged.connect(self._display_mode_changed)
        self.compare_combo.changed.connect(lambda _: self._compare_changed())
        self.split_slider.valueChanged.connect(self._split_changed)
        self.workspace_switch.changed.connect(self.set_workspace)
        self._zoom_changed(self.canvas.zoom)
        self._compare_changed()
        self._tool_settings_changed()

    @property
    def compress_level(self) -> int:
        try:
            return int(self.settings.value("compress_level", 6))
        except (TypeError, ValueError):
            return 6

    # ---------------------------------------------------------------- workspaces

    def _apply_workspace_defaults(self, name: str) -> None:
        visible = WORKSPACES[name][2]
        for key, dock in self.docks.items():
            dock.setVisible(key in visible)
        if name == "inspect":
            self.docks["hex"].raise_()
        self.resizeDocks([self.docks["inspector"]], [340], Qt.Orientation.Horizontal)
        if name == "edit":
            self.resizeDocks([self.docks["tool"], self.docks["inspector"], self.docks["history"]], [400, 360, 140],
                             Qt.Orientation.Vertical)
        if name == "inspect":
            self.resizeDocks([self.docks["hex"]], [280], Qt.Orientation.Vertical)

    def set_workspace(self, name: str, *, save_current: bool = True) -> None:
        if name not in WORKSPACES:
            name = "edit"
        if self.workspace == name:
            return
        if save_current and self.workspace is not None:
            self.settings.setValue(f"workspace_state/{self.workspace}", self.saveState())
        state = self.settings.value(f"workspace_state/{name}")
        if state is None or not self.restoreState(state):
            self._apply_workspace_defaults(name)
        self.workspace = name
        self.settings.setValue("workspace", name)
        self.act_workspaces[name].setChecked(True)
        self.workspace_switch.set_value(name, emit=False)
        if name == "inspect" and self.canvas.tool not in ("inspect", "hand", "zoom"):
            self.set_tool("inspect")

    def reset_workspace(self) -> None:
        self.settings.remove(f"workspace_state/{self.workspace}")
        self._apply_workspace_defaults(self.workspace)

    def set_theme(self, name: str) -> None:
        self.settings.setValue("theme", name)
        style.apply_theme(QApplication.instance(), name)
        restyle_chips()
        self._tool_settings_changed()
        self.info_panel.show_document(self.doc)
        self.document_changed(None)
        self.canvas.viewport().update()
        self.hex_panel.view.viewport().update()

    # ---------------------------------------------------------------- document

    def set_busy(self, busy: bool) -> None:
        self.canvas.busy = busy

    def set_tool(self, tool: str) -> None:
        self.canvas.set_tool(tool)
        self.act_tools[tool].setChecked(True)
        self.tool_panel.set_tool(tool)
        if tool in ("pencil", "select", "eyedropper") and self.workspace == "edit":
            self.docks["tool"].show()

    def set_document(self, doc: Document | None) -> None:
        self.doc = doc
        self.hashes = None
        self.hashed_at = None
        self.canvas.set_document(doc)
        self.tool_panel.set_mode(doc.mode if doc else None)
        if doc is not None:
            self._display_mode_changed()
            self._compare_changed()
            self.canvas.doc_source.highlight_changes = self.act_highlight.isChecked()
            self.canvas.zoom_to_fit()
        self.inspector.show_pixel(None, None)
        self.hex_panel.set_document(doc)
        self.info_panel.show_document(doc)
        self.document_changed(None)
        if doc is not None:
            self.set_inspected(0, 0)

    def document_changed(self, summary: OperationSummary | None, *, region: Rect | None = None) -> None:
        """Refresh views after an operation completes (never per mouse move): counters are
        O(1), the history list is short, and the full SHA-256 is debounced off-thread."""
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
            self.hex_panel.view.refresh()
        self.integrity_panel.show_document(doc, self.hashes, self.hashed_at)
        self.history_panel.show_document(doc)
        self._update_status()
        self._update_title()
        self._update_actions()
        self._selection_changed()

    def _update_status(self) -> None:
        doc = self.doc
        if doc is None:
            for w in (self.integrity_chip, self.save_chip):
                w.hide()
            self.doc_label.setText("")
            return
        state = integrity_state(doc, self.hashes)
        for chip, (kind, title, detail) in ((self.integrity_chip, state["original"]),
                                            (self.save_chip, state["saved"])):
            style.set_pill(chip, kind, title)
            chip.setToolTip(detail)
            chip.show()
        self.doc_label.setText(f"{doc.width} × {doc.height} {doc.mode}")

    def _update_title(self) -> None:
        doc = self.doc
        name = short_path(doc.path) if doc else "No image"
        self.setWindowTitle(f"{name}[*] — eXact {__version__}")
        self.setWindowModified(bool(doc and doc.dirty))

    def _update_actions(self) -> None:
        has = self.doc is not None
        for act in (self.act_save, self.act_save_as, self.act_select_all, self.act_goto, self.act_zoom_in,
                    self.act_zoom_out, self.act_zoom_100, self.act_zoom_fit, self.act_compare_disk,
                    *self.act_transforms.values()):
            act.setEnabled(has)
        self.act_compare_disk.setEnabled(bool(has and self.doc.path))
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
        self.act_undo.setToolTip(f"Undo {h.undo_label}  (Ctrl+Z)" if h and h.can_undo else "Nothing to undo")
        self.act_redo.setToolTip(f"Redo {h.redo_label}  (Ctrl+Shift+Z)" if h and h.can_redo else "Nothing to redo")
        self.act_restore_pixel.setEnabled(has and self.canvas.inspected is not None)

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
        self.hashed_at = time.time()
        self.integrity_panel.show_document(doc, hashes, self.hashed_at)
        self._update_status()

    # ---------------------------------------------------------------- view state

    def _zoom_changed(self, zoom: float) -> None:
        self.zoom_status.setText(zoom_label(zoom))
        idx = self.zoom_combo.findData(zoom)
        if idx >= 0:
            self.zoom_combo.setCurrentIndex(idx)

    def _hover_changed(self, pixel) -> None:
        if pixel is None or self.doc is None:
            self.pos_label.setText("")
            return
        x, y = pixel
        values = self.doc.get_pixel(x, y)
        names = self.doc.channel_names
        parts = "  ".join(f"{n} {v:3d}" for n, v in zip(names, values))
        self.pos_label.setText(f"x {x:<5} y {y:<5} {parts}  #{''.join(f'{v:02X}' for v in values)}")
        if self.hex_panel.follow.isChecked():
            self.hex_panel.show_bytes(self.doc, pixel_offset(x, y, self.doc.width, self.doc.channels),
                                      self.doc.channels)

    def _display_mode_changed(self) -> None:
        if self.canvas.doc_source:
            self.canvas.doc_source.display_mode = self.display_combo.currentData()
            self.canvas.viewport().update()

    def _compare_changed(self) -> None:
        mode = self.compare_combo.value()
        self.split_action.setVisible(mode == "split")
        self.act_show_original.blockSignals(True)
        self.act_show_original.setChecked(mode == "original")
        self.act_show_original.blockSignals(False)
        if self.canvas.doc_source:
            self.canvas.doc_source.compare = mode
            self.canvas.viewport().update()
        if mode == "original" and self.doc is not None:
            self.statusBar().showMessage("Showing ORIGINAL pixels (aligned to current orientation). "
                                         "Edits still apply to the current buffer.")

    def _toggle_original(self, on: bool) -> None:
        self.compare_combo.set_value("original" if on else "current")

    def _split_changed(self, value: int) -> None:
        if self.canvas.doc_source:
            self.canvas.doc_source.split = value / 1000
            self.canvas.viewport().update()

    def _set_highlight(self, on: bool) -> None:
        if self.canvas.doc_source:
            self.canvas.doc_source.highlight_changes = on
            self.canvas.viewport().update()

    def _tool_settings_changed(self) -> None:
        ts = self.tool_settings
        self.color_chip.setIcon(QIcon(swatch(ts.color, 14, self.color_chip)))
        self.color_chip.setToolTip(f"Pencil colour #{''.join(f'{v:02X}' for v in ts.color)}, size {ts.size}px. "
                                   "Click for pencil settings.")

    def _color_picked(self, values) -> None:
        self.tool_panel.set_color(values)
        hexed = "".join(f"{v:02X}" for v in values)
        self.statusBar().showMessage(f"Pencil colour set to #{hexed} (raw values, nothing modified)", 5000)

    def _nudge_size(self, delta: int) -> None:
        self.tool_panel.size_spin.setValue(self.tool_panel.size_spin.value() + delta)

    def _selection_changed(self) -> None:
        doc = self.doc
        self.tool_panel.set_selection(self.canvas.selection, doc.width if doc else 0, doc.height if doc else 0,
                                      has_clip=bool(doc and self.clipboard is not None))
        self._update_actions_selection()

    def _update_actions_selection(self) -> None:
        has_sel = self.doc is not None and self.canvas.selection is not None
        for act in (self.act_copy, self.act_fill, self.act_deselect):
            act.setEnabled(has_sel)

    def _selection_edited(self, rect: Rect) -> None:
        if self.doc is None:
            return
        rect = rect.clamp(self.doc.width, self.doc.height)
        self.canvas.set_selection(None if rect.empty else rect)

    def _tool_action(self, key: str) -> None:
        {"select_all": self.select_all, "deselect": self.deselect, "copy": self.copy, "paste": self.paste,
         "paste_at": self.paste_at, "fill": self.fill_selection}[key]()

    def set_inspected(self, x: int, y: int) -> None:
        if self.doc is None:
            return
        self.canvas.set_inspected((x, y))
        self.inspector.show_pixel(self.doc, (x, y))
        if not self.hex_panel.follow.isChecked():
            self.hex_panel.show_bytes(self.doc, pixel_offset(x, y, self.doc.width, self.doc.channels),
                                      self.doc.channels)
        self.act_restore_pixel.setEnabled(True)

    def _goto_offset(self, offset: int) -> None:
        doc = self.doc
        if doc is None or offset >= doc.pixels.size:
            self.statusBar().showMessage(
                f"Offset outside the pixel buffer (0 … {doc.pixels.size - 1:,})" if doc else "No image open", 5000)
            return
        index = offset // doc.channels
        x, y = index % doc.width, index // doc.width
        self.canvas.set_inspected((x, y))
        self.inspector.show_pixel(doc, (x, y))
        self.hex_panel.view.highlight = (index * doc.channels, doc.channels)
        self.hex_panel.view.cursor = offset
        self.hex_panel.show_bytes(doc, offset, 0)
        self.hex_panel.view.highlight = (index * doc.channels, doc.channels)
        self.canvas.center_on(x + 0.5, y + 0.5)

    def _goto_xy(self, x: int, y: int) -> None:
        if self.doc is None:
            return
        if not (0 <= x < self.doc.width and 0 <= y < self.doc.height):
            self.statusBar().showMessage(f"({x}, {y}) is outside the {self.doc.width}×{self.doc.height} image", 5000)
            return
        self.set_inspected(x, y)
        self.canvas.center_on(x + 0.5, y + 0.5)

    # ---------------------------------------------------------------- editing

    def _stroke_finished(self, summary: OperationSummary) -> None:
        self.document_changed(summary)

    def _apply_pixel(self, x: int, y: int, values) -> None:
        if self.doc is None:
            return
        summary = self.doc.set_pixel(x, y, values)
        self.document_changed(summary, region=Rect(x, y, 1, 1))

    def restore_inspected_pixel(self) -> None:
        if self.doc is None or self.canvas.inspected is None:
            return
        x, y = self.canvas.inspected
        original = tuple(int(v) for v in self.doc.reference()[y, x])
        if original == self.doc.get_pixel(x, y):
            self.statusBar().showMessage(f"({x}, {y}) already matches the original", 4000)
            return
        summary = self.doc.set_pixel(x, y, original, label=f"Restore pixel ({x}, {y})")
        self.document_changed(summary, region=Rect(x, y, 1, 1))

    def jump_to_history(self, position: int) -> None:
        """Undo or redo until *position* commands are applied."""
        if self.doc is None:
            return
        current = len(self.doc.history)
        for _ in range(abs(position - current)):
            (self.undo if position < current else self.redo)()

    # ---------------------------------------------------------------- files

    def open_dialog(self) -> None:
        if not self.maybe_save_changes():
            return
        start = os.path.dirname(self.doc.path) if self.doc and self.doc.path else str(
            self.settings.value("last_dir", ""))
        path, _ = QFileDialog.getOpenFileName(self, "Open PNG", start, "PNG images (*.png);;All files (*)")
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> bool:
        name = os.path.basename(path)
        try:
            doc = run_blocking(self, f"Opening {name}…", Document.open, path, cancellable=True)
        except TaskCancelled:
            self.statusBar().showMessage(f"Opening {name} was cancelled; nothing changed.", 6000)
            return False
        except Exception as exc:  # explained to the user; nothing was opened or converted
            show_error(self, exc, "open", note=f"{name} was not opened. No conversion was performed.")
            return False
        self.settings.setValue("last_dir", os.path.dirname(os.path.abspath(path)))
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
        except Exception as exc:
            if interactive:
                show_error(self, exc, "save", note=f"{short_path(path)} was NOT modified.")
            else:
                self.statusBar().showMessage(f"Save failed: {exc}. {short_path(path)} was NOT modified.")
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

    def compare_with_disk(self) -> None:
        if self.doc is None or not self.doc.path:
            return
        dialog = DiffDialog(self, self.doc, path_b=self.doc.path, use_current=True)
        dialog.run_compare()
        dialog.exec()

    # ---------------------------------------------------------------- transforms

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

    # ---------------------------------------------------------------- selection & clipboard

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
        self._selection_changed()

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

    # ---------------------------------------------------------------- help

    def about(self) -> None:
        QMessageBox.about(
            self, "About eXact",
            f"<h2>eXact {__version__}</h2><p><b>eXtensible Accurate Color Toolkit</b></p>"
            f"<p><i>{TAGLINE}</i></p><p>A binary-preserving pixel editor for 8-bit RGB/RGBA PNGs.</p>"
            f"<p><b>{style.MOTTO}</b><br><i>{style.MOTTO_FOOTNOTE}</i></p><p>{MOTTO}</p>")

    def show_guarantees(self) -> None:
        QMessageBox.information(self, "Preservation guarantees", GUARANTEES_TEXT)

    def show_shortcuts(self) -> None:
        rows = []
        for title, actions in (
                ("Tools", list(self.act_tools.values()) + [self.act_size_down, self.act_size_up]),
                ("File", [self.act_open, self.act_save, self.act_save_as, self.act_compare]),
                ("Edit", [self.act_undo, self.act_redo, self.act_copy, self.act_paste, self.act_paste_at,
                          self.act_fill, self.act_select_all, self.act_deselect, self.act_goto]),
                ("View", [self.act_zoom_in, self.act_zoom_out, self.act_zoom_100, self.act_zoom_fit, self.act_grid,
                          self.act_show_original, self.act_highlight, *self.act_workspaces.values()]),
                ("Image", [self.act_transforms[transforms.ROTATE_CW], self.act_transforms[transforms.ROTATE_CCW]])):
            rows.append(f"<tr><td colspan=2><br><b>{title}</b></td></tr>")
            for act in actions:
                keys = act.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
                if keys:
                    name = act.text().replace("&", "").split(" Undo ")[0]
                    rows.append(f"<tr><td style='padding-right:24px'>{name}</td><td><tt>{keys}</tt></td></tr>")
        rows.append("<tr><td colspan=2><br><b>Canvas</b></td></tr>")
        for name, keys in (("Pan", "Space+drag, middle-drag, scroll"), ("Zoom at cursor", "Ctrl+scroll"),
                           ("Pick colour (Pencil)", "Alt+click"), ("Cancel paste", "Esc / right-click")):
            rows.append(f"<tr><td style='padding-right:24px'>{name}</td><td><tt>{keys}</tt></td></tr>")
        QMessageBox.information(self, "Keyboard shortcuts", "<table>" + "".join(rows) + "</table>")

    # ---------------------------------------------------------------- window events

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if urls and self.maybe_save_changes():
            self.open_path(urls[0].toLocalFile())

    def closeEvent(self, event) -> None:
        if self.maybe_save_changes():
            if self.workspace is not None:
                self.settings.setValue(f"workspace_state/{self.workspace}", self.saveState())
            event.accept()
        else:
            event.ignore()
