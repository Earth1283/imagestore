"""The ImageStore window: Encode, Decode and Inspect, each a page in one workflow sidebar."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QSettings, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QDesktopServices, QGuiApplication, QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import imagestore as core

from . import style
from .widgets import (
    Banner,
    DropZone,
    KeyValueGrid,
    ProgressRow,
    Runner,
    Segmented,
    SizeBars,
    SourceCard,
    StatTile,
    button,
    card,
    label,
)

CONTENT_WIDTH = 860


def reveal(path: Path) -> None:
    """Open the folder containing *path* in the system file manager."""
    folder = path if path.is_dir() else path.parent
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def is_png(path: Path) -> bool:
    return path.suffix.lower() == ".png"


def scan_folder(path: Path, task) -> tuple[int, int, list]:
    """Count files and bytes of a folder and find auto-ignore candidates (cancellable)."""
    task.phase("Scanning folder")
    files = size = 0
    for dirpath, _dirs, filenames in os.walk(path):
        task.check()
        for name in filenames:
            files += 1
            try:
                size += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    task.check()
    return files, size, core.scan_ignored_detail(path)


def peek_archive(path: Path, task):
    """Header, dimensions and file size of an image without decoding its payload."""
    task.phase("Reading header")
    img, meta = core.read_header(path)
    return meta, img.size, path.stat().st_size


class Page(QWidget):
    """Common page scaffolding: header, centred scrolling column, runner, banner."""

    title_text = ""
    subtitle_text = ""
    status = pyqtSignal(str)

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.runner = Runner(self)
        self.runner.progress.connect(self._progress)
        self.runner.finished.connect(self._finished)
        self.runner.failed.connect(self._failed)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        self.scroll = scroll
        holder = QWidget()
        holder.setProperty("role", "panel")
        scroll.setWidget(holder)
        row = QHBoxLayout(holder)
        row.setContentsMargins(28, 24, 28, 28)
        row.addStretch(1)
        column = QWidget()
        column.setMaximumWidth(CONTENT_WIDTH)
        column.setMinimumWidth(min(CONTENT_WIDTH, 460))
        row.addWidget(column, 100)
        row.addStretch(1)
        self.body = QVBoxLayout(column)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(14)
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(label(self.title_text, "display"))
        head.addWidget(label(self.subtitle_text, "muted", wrap=True))
        self.body.addLayout(head)
        self.body.addSpacing(4)
        self.progress = ProgressRow()
        self.progress.cancelRequested.connect(self.cancel)
        self.banner = Banner()

    def finish_layout(self) -> None:
        self.body.addStretch(1)

    @property
    def busy(self) -> bool:
        return self.runner.busy

    def cancel(self) -> None:
        if self.runner.busy:
            self.runner.cancel()
            self.progress.cancelling()

    def _progress(self, text: str, done, total) -> None:
        self.progress.update_progress(text, done, total)

    def _finished(self, result) -> None:  # overridden
        self.progress.end()

    def reveal_widget(self, widget: QWidget) -> None:
        """Scroll so a result just shown is in view (after layout has caught up)."""
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(widget, 0, 24))

    def _failed(self, exc) -> None:
        self.progress.end()
        self.banner.show_error(exc)
        self.reveal_widget(self.banner)
        self.status.emit(core.explain(exc).title)
        self.refresh()

    def refresh(self) -> None:  # overridden: recompute enabled states
        pass

    def primary(self) -> None:  # overridden: the page's main action (Ctrl+Return)
        pass

    def choose(self) -> None:  # overridden: pick an input (Ctrl+O)
        pass

    def handle_paths(self, paths: list[Path]) -> None:  # overridden: window-level drops
        pass


# --------------------------------------------------------------------------- encode


WINDOW_CHOICES = (1, 4, 16, 64, 256, 1024)  # MiB


class EncodePage(Page):
    title_text = "Encode"
    subtitle_text = "Turn a file, a folder or some text into a lossless RGBA PNG. Every byte is recoverable."
    inspectRequested = pyqtSignal(Path)

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(settings, parent)
        self.source: Path | None = None
        self.hits: list = []
        self._output_touched = False
        self.scanner = Runner(self)
        self.scanner.finished.connect(self._scanned)
        self.scanner.failed.connect(self._scan_failed)

        self.kind = Segmented([("path", "File or folder", "file"), ("text", "Text", "text")])
        self.kind.changed.connect(self._kind_changed)
        self.body.addWidget(self.kind)

        self.drop = DropZone("encode", "Drop a file or folder here",
                             "Anything works: documents, executables, databases, whole project folders.")
        self.drop.add_button(button("Choose file…", "file")).clicked.connect(self._choose_file)
        self.drop.add_button(button("Choose folder…", "folder")).clicked.connect(self._choose_folder)
        self.drop.dropped.connect(lambda paths: self.set_source(paths[0]))
        self.body.addWidget(self.drop)

        self.source_card = SourceCard()
        self.source_card.change.clicked.connect(self.choose)
        self.source_card.cleared.connect(lambda: self.set_source(None))
        self.source_card.hide()
        self.body.addWidget(self.source_card)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Type or paste text to store. It is encoded as UTF-8.")
        self.text.setMinimumHeight(160)
        self.text.textChanged.connect(self._text_changed)
        self.text_count = label("", "faint")
        self.text_box = QWidget()
        tb = QVBoxLayout(self.text_box)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(4)
        tb.addWidget(self.text)
        tb.addWidget(self.text_count, 0, Qt.AlignmentFlag.AlignRight)
        self.text_box.hide()
        self.body.addWidget(self.text_box)

        self._build_ignore_card()
        self._build_options()

        actions = QHBoxLayout()
        self.go = button("Encode to PNG", "encode", "primary")
        self.go.setToolTip("Encode, write, then decode the PNG again to verify it (Ctrl+Return)")
        self.go.clicked.connect(self.primary)
        actions.addWidget(self.go)
        actions.addSpacing(12)
        actions.addWidget(self.progress, 1)
        self.go.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        actions.addStretch(1)
        self.body.addLayout(actions)
        self.body.addWidget(self.banner)
        self._build_result()
        self.finish_layout()
        self.refresh()

    # -- layout

    def _build_ignore_card(self) -> None:
        self.ignore_card, lay = card()
        head = QHBoxLayout()
        head.addWidget(label("Auto-ignore", "title"))
        self.ignore_pill = QLabel()
        head.addWidget(self.ignore_pill)
        head.addStretch(1)
        lay.addLayout(head)
        lay.addWidget(label("These entries match common junk patterns (.DS_Store, node_modules, __pycache__, .git, "
                            ".venv…). Checked entries are left out of the archive; uncheck any you want to keep.",
                            "muted", wrap=True))
        self.ignore_all = QCheckBox("Skip all ignorable entries")
        self.ignore_all.setTristate(False)
        self.ignore_all.clicked.connect(self._ignore_all_clicked)
        lay.addWidget(self.ignore_all)
        self.ignore_tree = QTreeWidget()
        self.ignore_tree.setRootIsDecorated(False)
        self.ignore_tree.setAlternatingRowColors(True)
        self.ignore_tree.setHeaderLabels(["Entry", "Kind", "Files", "Size"])
        self.ignore_tree.setUniformRowHeights(True)
        self.ignore_tree.setMaximumHeight(240)
        header = self.ignore_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        for col in (2, 3):
            self.ignore_tree.headerItem().setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.ignore_tree.itemChanged.connect(self._ignore_item_changed)
        lay.addWidget(self.ignore_tree)
        self.ignore_card.hide()
        self.body.addWidget(self.ignore_card)

    def _build_options(self) -> None:
        frame, lay = card()
        lay.addWidget(label("Output", "section"))
        row = QHBoxLayout()
        self.output = QLineEdit()
        self.output.setPlaceholderText("Choose where the PNG goes")
        self.output.setAccessibleName("Output PNG path")
        self.output.textEdited.connect(self._output_edited)
        browse = button("Browse…", "open")
        browse.clicked.connect(self._choose_output)
        row.addWidget(self.output, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        self.output_note = label("", "faint", wrap=True)
        lay.addWidget(self.output_note)
        self.overwrite = QCheckBox("Overwrite if the PNG already exists")
        self.overwrite.toggled.connect(lambda _: self.refresh())
        lay.addWidget(self.overwrite)
        lay.addWidget(self._divider())
        lay.addWidget(label("Options", "section"))
        self.compress = QCheckBox("Compress first (zlib or LZMA)")
        self.compress.setChecked(self.settings.value("compress", True, type=bool))
        self.compress.toggled.connect(lambda on: self.settings.setValue("compress", on))
        lay.addWidget(self.compress)
        lay.addWidget(self._indent(label("Kept only when it makes the payload smaller; already-compressed data is "
                                         "stored raw.", "faint", wrap=True)))
        self.window_size = QComboBox()
        self.window_size.setAccessibleName("LZMA window size")
        for mib in WINDOW_CHOICES:
            self.window_size.addItem(f"{mib} MiB" + ("  (default)" if mib == core.LZMA_WINDOW >> 20 else ""), mib)
        saved = self.settings.value("window_mib", core.LZMA_WINDOW >> 20, type=int)
        self.window_size.setCurrentIndex(max(0, self.window_size.findData(saved)))
        self.window_size.currentIndexChanged.connect(lambda _: self.settings.setValue("window_mib", self.window_mib()))
        self.compress.toggled.connect(self.window_size.setEnabled)
        self.window_size.setEnabled(self.compress.isChecked())
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(label("LZMA window", "key"))
        row.addWidget(self.window_size)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        lay.addWidget(self._indent(holder))
        self.ecc = QSpinBox()
        self.ecc.setAccessibleName("Error correction, percent of extra space")
        self.ecc.setRange(0, 50)
        self.ecc.setSuffix(" %")
        self.ecc.setSpecialValueText("Off")
        self.ecc.setValue(min(50, max(0, self.settings.value("ecc_percent", 0, type=int))))
        self.ecc_note = label("", "faint", wrap=True)
        self.ecc.valueChanged.connect(self._ecc_changed)
        self._ecc_changed(self.ecc.value())
        lay.addWidget(self._indent(label("How far back LZMA looks for repeats. A larger window can shrink big "
                                         "folders with far-apart repeats but needs about 7× its size in RAM while "
                                         "encoding. Small inputs never use more than they need.", "faint", wrap=True)))
        ecc_row = QHBoxLayout()
        ecc_row.setContentsMargins(0, 0, 0, 0)
        ecc_row.addWidget(label("Error correction", "key"))
        ecc_row.addWidget(self.ecc)
        ecc_row.addStretch(1)
        holder = QWidget()
        holder.setLayout(ecc_row)
        lay.addWidget(holder)
        lay.addWidget(self.ecc_note)
        self.verify = QCheckBox("Verify after writing")
        self.verify.setChecked(True)
        lay.addWidget(self.verify)
        lay.addWidget(self._indent(label("Decode the written PNG and check its SHA-256 before reporting success. "
                                         "Recommended.", "faint", wrap=True)))
        self.body.addWidget(frame)

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setProperty("role", "divider")
        return line

    def _ecc_changed(self, percent: int) -> None:
        self.settings.setValue("ecc_percent", percent)
        if not percent:
            self.ecc_note.setText("Off. If some pixels are damaged later, the image cannot be repaired.")
            return
        e = core.EccReport(core.ecc_parity_bytes(percent), 0)
        self.ecc_note.setText(f"Adds about {e.overhead:.1%} to the image and repairs damage to up to "
                              f"{e.tolerance:.1%} of it. Encoding and opening take longer.")

    def window_mib(self) -> int:
        """The chosen LZMA window cap in MiB."""
        return self.window_size.currentData()

    @staticmethod
    def _indent(widget: QWidget) -> QWidget:
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(26, 0, 0, 0)
        lay.addWidget(widget)
        return holder

    def _build_result(self) -> None:
        self.result_card, lay = card()
        grid = QGridLayout()
        grid.setSpacing(10)
        self.tile_grid = grid
        self.tiles = {}
        for i, (key, caption) in enumerate((("orig", "Original"), ("stored", "Stored payload"), ("png", "PNG file"),
                                            ("ratio", "Compression"), ("dims", "Dimensions"), ("pixels", "Pixels"),
                                            ("padding", "Padding"), ("check", "Checksum"), ("dupes", "Duplicates"), ("ecc", "Error correction"))):
            tile = StatTile(caption)
            self.tiles[key] = tile
            grid.addWidget(tile, i // 4, i % 4)
        lay.addLayout(grid)
        self.size_bars = SizeBars()
        lay.addWidget(self.size_bars)
        sha_row = QHBoxLayout()
        sha_row.addWidget(label("SHA-256", "key"))
        self.sha_value = label("", "value", selectable=True, mono=True)
        self.sha_value.setWordWrap(True)
        sha_row.addWidget(self.sha_value, 1)
        copy = button("Copy", "copy", "flat")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.sha_value.text()))
        sha_row.addWidget(copy)
        lay.addLayout(sha_row)
        actions = QHBoxLayout()
        self.reveal_btn = button("Show in folder", "reveal")
        self.inspect_btn = button("Inspect", "verify")
        actions.addWidget(self.reveal_btn)
        actions.addWidget(self.inspect_btn)
        actions.addStretch(1)
        lay.addLayout(actions)
        self.result_card.hide()
        self.body.addWidget(self.result_card)
        self._last_out: Path | None = None
        self.reveal_btn.clicked.connect(lambda: self._last_out and reveal(self._last_out))
        self.inspect_btn.clicked.connect(lambda: self._last_out and self.inspectRequested.emit(self._last_out))

    # -- state

    def _kind_changed(self, kind: str) -> None:
        text = kind == "text"
        self.text_box.setVisible(text)
        self.drop.setVisible(not text and self.source is None)
        self.source_card.setVisible(not text and self.source is not None)
        self.ignore_card.setVisible(not text and bool(self.hits))
        if not self._output_touched:
            self._default_output()
        if text:
            self.text.setFocus()
        self.refresh()

    def set_source(self, path: Path | None) -> None:
        if self.busy:
            return
        self.scanner.cancel()
        self.source = Path(path).resolve() if path else None
        self.hits = []
        self.ignore_card.hide()
        self.banner.hide()
        self.result_card.hide()
        self._output_touched = False
        if self.kind.value() != "path":
            self.kind.set_value("path")
        if self.source is None:
            self.drop.show()
            self.source_card.hide()
        else:
            self.drop.hide()
            self.source_card.show()
            if self.source.is_dir():
                self.source_card.show_source("folder", self.source.name + "/", f"{self.source}  ·  scanning…")
                self.scanner.wait(2)
                self.scanner.start(scan_folder, self.source)
            elif self.source.is_file():
                size = self.source.stat().st_size
                self.source_card.show_source("file", self.source.name, f"{self.source}  ·  {core.human(size)}")
            else:
                self.banner.show_error(core.ImageStoreError(f"no such file or folder: {self.source}", "missing"))
                self.source = None
                self.drop.show()
                self.source_card.hide()
        self._default_output()
        self.refresh()

    def _scanned(self, result) -> None:
        if self.source is None or not self.source.is_dir():
            return
        files, size, hits = result
        self.source_card.show_source("folder", self.source.name + "/",
                                     f"{self.source}  ·  {files:,} files, {core.human(size)}")
        self.hits = hits
        self.ignore_tree.blockSignals(True)
        self.ignore_tree.clear()
        for hit in hits:
            item = QTreeWidgetItem([hit.rel + ("/" if hit.is_dir else ""), "folder" if hit.is_dir else "file",
                                    f"{hit.files:,}", core.human(hit.size)])
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, hit.rel)
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setTextAlignment(3, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.ignore_tree.addTopLevelItem(item)
        self.ignore_tree.blockSignals(False)
        rows = max(1, min(len(hits), 8))
        self.ignore_tree.setFixedHeight(self.ignore_tree.header().sizeHint().height() + 6
                                        + rows * max(22, self.ignore_tree.sizeHintForRow(0)))
        self.ignore_card.setVisible(bool(hits) and self.kind.value() == "path")
        self._sync_ignore_summary()

    def _scan_failed(self, exc) -> None:
        if getattr(exc, "kind", "") != "cancelled":
            self.banner.show_error(exc)

    def excluded(self) -> set[str]:
        out = set()
        for i in range(self.ignore_tree.topLevelItemCount()):
            item = self.ignore_tree.topLevelItem(i)
            if item.checkState(0) == Qt.CheckState.Checked:
                out.add(item.data(0, Qt.ItemDataRole.UserRole))
        return out

    def _ignore_all_clicked(self, on: bool) -> None:
        self.ignore_tree.blockSignals(True)
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for i in range(self.ignore_tree.topLevelItemCount()):
            self.ignore_tree.topLevelItem(i).setCheckState(0, state)
        self.ignore_tree.blockSignals(False)
        self._sync_ignore_summary()

    def _ignore_item_changed(self, *_):
        self._sync_ignore_summary()

    def _sync_ignore_summary(self) -> None:
        skipped = self.excluded()
        chosen = [h for h in self.hits if h.rel in skipped]
        n = len(self.hits)
        self.ignore_all.setChecked(len(chosen) == n and n > 0)
        if chosen:
            style.set_pill(self.ignore_pill, "warn", f"skipping {len(chosen)} of {n} · "
                           f"{sum(h.files for h in chosen):,} files · {core.human(sum(h.size for h in chosen))}")
        else:
            style.set_pill(self.ignore_pill, "neutral", f"keeping all {n}")

    def _text_changed(self) -> None:
        text = self.text.toPlainText()
        self.text_count.setText(f"{len(text):,} characters · {core.human(len(text.encode('utf-8')))} UTF-8")
        self.refresh()

    def _default_output(self) -> None:
        if self.kind.value() == "text":
            base = Path(self.settings.value("last_dir", str(Path.home())))
            self.output.setText(str(base / "text.png"))
        elif self.source is not None:
            self.output.setText(str(core.default_png_path(self.source)))
        else:
            self.output.clear()
        self.refresh()

    def _output_edited(self, _text: str) -> None:
        self._output_touched = True
        self.refresh()

    def output_path(self) -> Path | None:
        text = self.output.text().strip()
        return Path(text).expanduser() if text else None

    def refresh(self) -> None:
        out = self.output_path()
        note, ok = "", out is not None
        if out is not None:
            if out.is_dir():
                note, ok = "That is a folder. Add a file name such as archive.png.", False
            elif not out.absolute().parent.is_dir():
                note, ok = f"The folder {out.absolute().parent} does not exist.", False
            elif out.exists():
                note = "A file with this name exists." + ("" if self.overwrite.isChecked()
                                                          else " Tick Overwrite to replace it.")
                ok = self.overwrite.isChecked()
            elif out.suffix.lower() != ".png":
                note = "Tip: use a .png extension so other apps recognise the file."
        self.output_note.setText(note)
        self.output_note.setVisible(bool(note))
        impossible = out is not None and not ok and not out.exists()
        self.output.setProperty("invalid", "true" if impossible or (out is not None and out.is_dir()) else "false")
        style.repolish(self.output)
        has_input = (self.kind.value() == "text" and bool(self.text.toPlainText())) or (
            self.kind.value() == "path" and self.source is not None)
        self.go.setEnabled(has_input and ok and not self.busy)

    # -- actions

    def choose(self) -> None:
        if self.kind.value() == "text":
            self.text.setFocus()
        else:
            self._choose_file()

    def _start_dir(self) -> str:
        return str(self.source.parent if self.source else self.settings.value("last_dir", str(Path.home())))

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file to encode", self._start_dir())
        if path:
            self.set_source(Path(path))

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder to encode", self._start_dir())
        if path:
            self.set_source(Path(path))

    def _choose_output(self) -> None:
        current = self.output.text() or self._start_dir()
        path, _ = QFileDialog.getSaveFileName(self, "Save PNG as", current, "PNG images (*.png)",
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            self.output.setText(path)
            self._output_touched = True
            self.refresh()

    def handle_paths(self, paths: list[Path]) -> None:
        self.set_source(paths[0])

    def primary(self) -> None:
        if not self.go.isEnabled():
            return
        out = self.output_path()
        self.banner.hide()
        self.result_card.hide()
        self.progress.begin()
        common = dict(compress=self.compress.isChecked(), window=self.window_mib() << 20, ecc=self.ecc.value(), force=self.overwrite.isChecked(),
                      verify=self.verify.isChecked())
        if self.kind.value() == "text":
            self.runner.start(core.encode_text, self.text.toPlainText(), out, **common)
        else:
            exclude = self.excluded() if self.source.is_dir() else None
            self.runner.start(core.encode_path, self.source, out, exclude=exclude or None, **common)
        self.settings.setValue("last_dir", str(out.absolute().parent))
        self.status.emit("Encoding…")
        self.refresh()

    def _finished(self, r) -> None:
        super()._finished(r)
        m = r.meta
        self._last_out = r.out
        verified = "verified" if r.verified else "written (not verified)"
        self.banner.show_message("ok" if r.verified else "warn", f"Encoded and {verified}",
                                 f"{core.show_path(r.out)}")
        t = self.tiles
        t["orig"].set(core.human(m.orig_size), f"{m.orig_size:,} bytes")
        t["stored"].set(core.human(m.stored_size), "zero padding removed" if m.squeezed else m.method)
        t["png"].set(core.human(r.png_size), f"{r.png_size:,} bytes")
        if m.compressed or m.squeezed:
            t["ratio"].set(f"{r.saved_fraction:.1%}", "smaller than the original")
        else:
            t["ratio"].set("None", "compression would not shrink it" if r.compress_requested else "compression off")
        t["dims"].set(f"{r.width} × {r.height}", "RGBA, 8-bit")
        t["pixels"].set(f"{r.width * r.height:,}", "4 bytes each")
        t["padding"].set(core.human(r.padding), "zero bytes after the payload")
        if m.ecc:
            t["ecc"].set(f"{m.ecc.overhead:.1%}", f"repairs up to ~{m.ecc.tolerance:.1%} damage")
        extras = [key for key, shown in (("dupes", r.duplicates), ("ecc", m.ecc)) if shown]
        for key in ("dupes", "ecc"):  # the tiles that only sometimes apply fill the last row from the left
            self.tile_grid.removeWidget(t[key])
            t[key].setVisible(key in extras)
        for n, key in enumerate(extras):
            self.tile_grid.addWidget(t[key], 2, n)
        if r.duplicates:
            t["dupes"].set(f"{r.duplicates:,} {'file' if r.duplicates == 1 else 'files'}",
                           f"identical to another · {core.human(r.duplicate_bytes)} not stored")
        t["check"].set("✓ Match" if r.verified else "Skipped", "SHA-256 re-checked" if r.verified else "")
        t["check"].value.setStyleSheet(f"color: {style.tokens()['success' if r.verified else 'warning']};")
        self.size_bars.set_sizes([("Original", m.orig_size, "text_faint"), ("Stored", m.stored_size, "accent"),
                                  ("PNG file", r.png_size, "success")])
        self.sha_value.setText(m.digest.hex())
        self.result_card.show()
        self.reveal_widget(self.result_card)
        self.status.emit(f"Encoded {r.out.name} ({core.human(r.png_size)})")
        self.refresh()


# --------------------------------------------------------------------------- decode


class DecodePage(Page):
    title_text = "Decode"
    subtitle_text = "Restore the original file, folder or text from an ImageStore PNG, checked against its SHA-256."

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(settings, parent)
        self.image: Path | None = None
        self.meta = None
        self.peeker = Runner(self)
        self.peeker.finished.connect(self._peeked)
        self.peeker.failed.connect(self._peek_failed)

        self.drop = DropZone("decode", "Drop an ImageStore PNG here",
                             "The original comes back byte for byte, or not at all.")
        self.drop.accepts = lambda p: p.is_file() and is_png(p)
        self.drop.add_button(button("Choose image…", "image")).clicked.connect(self.choose)
        self.drop.dropped.connect(lambda paths: self.set_image(paths[0]))
        self.body.addWidget(self.drop)
        self.source_card = SourceCard()
        self.source_card.change.clicked.connect(self.choose)
        self.source_card.cleared.connect(lambda: self.set_image(None))
        self.source_card.hide()
        self.body.addWidget(self.source_card)

        self.options, lay = card()
        lay.addWidget(label("Restore into", "section"))
        row = QHBoxLayout()
        self.dest = QLineEdit()
        self.dest.setAccessibleName("Destination folder")
        self.dest.textEdited.connect(lambda _: self.refresh())
        browse = button("Browse…", "open")
        browse.clicked.connect(self._choose_dest)
        row.addWidget(self.dest, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        self.dest_note = label("", "faint", wrap=True)
        lay.addWidget(self.dest_note)
        self.overwrite = QCheckBox("Overwrite (or merge into) existing output")
        self.overwrite.toggled.connect(lambda _: self.refresh())
        lay.addWidget(self.overwrite)
        self.text_options = QWidget()
        tl = QVBoxLayout(self.text_options)
        tl.setContentsMargins(0, 4, 0, 0)
        self.text_show = QRadioButton("Show the text here")
        self.text_save = QRadioButton("Save it as text.txt in the folder above")
        self.text_show.setChecked(True)
        self.text_show.toggled.connect(lambda _: self.refresh())
        tl.addWidget(self.text_show)
        tl.addWidget(self.text_save)
        lay.addWidget(self.text_options)
        self.options.hide()
        self.body.addWidget(self.options)

        actions = QHBoxLayout()
        self.go = button("Decode", "decode", "primary")
        self.go.setToolTip("Decode and verify, then write the original (Ctrl+Return)")
        self.go.clicked.connect(self.primary)
        actions.addWidget(self.go)
        actions.addSpacing(12)
        actions.addWidget(self.progress, 1)
        self.go.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        actions.addStretch(1)
        self.body.addLayout(actions)
        self.body.addWidget(self.banner)

        self.result_card, lay = card()
        self.result_grid = KeyValueGrid()
        lay.addWidget(self.result_grid)
        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setMinimumHeight(160)
        lay.addWidget(self.text_view)
        acts = QHBoxLayout()
        self.reveal_btn = button("Show in folder", "reveal")
        self.copy_btn = button("Copy text", "copy")
        self.copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text_view.toPlainText()))
        acts.addWidget(self.reveal_btn)
        acts.addWidget(self.copy_btn)
        acts.addStretch(1)
        lay.addLayout(acts)
        self._last_out: Path | None = None
        self.reveal_btn.clicked.connect(lambda: self._last_out and reveal(self._last_out))
        self.result_card.hide()
        self.body.addWidget(self.result_card)
        self.finish_layout()
        self.refresh()

    def set_image(self, path: Path | None) -> None:
        if self.busy:
            return
        self.peeker.cancel()
        self.image = Path(path).resolve() if path else None
        self.meta = None
        self.banner.hide()
        self.result_card.hide()
        self.options.hide()
        if self.image is None:
            self.drop.show()
            self.source_card.hide()
        else:
            self.drop.hide()
            self.source_card.show()
            self.source_card.show_source("image", self.image.name, f"{self.image}  ·  reading header…")
            self.dest.setText(str(self.image.parent))
            self.peeker.wait(5)
            self.peeker.start(peek_archive, self.image)
        self.refresh()

    def _peeked(self, result) -> None:
        meta, (w, h), size = result
        self.meta = meta
        kind = "text" if meta.is_text else "folder" if meta.is_dir else "file"
        what = "text" if meta.is_text else f"{kind} “{meta.name}”"
        self.source_card.show_source(
            {"text": "text", "folder": "folder", "file": "file"}[kind], self.image.name,
            f"Contains {what} · {core.human(meta.orig_size)}{' · ' + meta.method if meta.method != 'raw' else ''} · "
            f"{w}×{h} px, {core.human(size)} PNG")
        self.text_options.setVisible(meta.is_text)
        self.options.show()
        self.refresh()

    def _peek_failed(self, exc) -> None:
        if getattr(exc, "kind", "") == "cancelled":
            return
        self.banner.show_error(exc)
        if self.image is not None:
            self.source_card.show_source("image", self.image.name, str(self.image))
        self.refresh()

    def _target(self) -> Path | None:
        if self.meta is None or not self.dest.text().strip():
            return None
        return Path(self.dest.text().strip()).expanduser() / core.safe_name(self.meta.name)

    def _to_memory(self) -> bool:
        return bool(self.meta and self.meta.is_text and self.text_show.isChecked())

    def refresh(self) -> None:
        ok = self.meta is not None and not self.busy and not self.peeker.busy
        note = ""
        target = self._target()
        if ok and not self._to_memory():
            if target is None:
                ok, note = False, "Choose a folder to restore into."
            elif target.exists():
                if target.is_dir() != self.meta.is_dir:
                    ok, note = False, f"{target.name} exists as a different type (file vs folder)."
                else:
                    verb = "merged into" if self.meta.is_dir else "replaced"
                    note = f"{target.name} already exists and will be {verb}." if self.overwrite.isChecked() \
                        else f"{target.name} already exists there. Tick Overwrite, or choose another folder."
                    ok = self.overwrite.isChecked()
            else:
                note = f"Will create {target}{'/' if self.meta.is_dir else ''}"
        self.dest_note.setText(note)
        self.dest_note.setVisible(bool(note))
        self.dest.setEnabled(not self._to_memory())
        self.go.setEnabled(ok)

    def choose(self) -> None:
        start = str(self.image.parent if self.image else self.settings.value("last_dir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(self, "Choose an ImageStore PNG", start,
                                              "PNG images (*.png);;All files (*)")
        if path:
            self.set_image(Path(path))

    def _choose_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Restore into", self.dest.text())
        if path:
            self.dest.setText(path)
            self.refresh()

    def handle_paths(self, paths: list[Path]) -> None:
        self.set_image(paths[0])

    def primary(self) -> None:
        if not self.go.isEnabled():
            return
        self.banner.hide()
        self.result_card.hide()
        self.progress.begin()
        to_memory = self._to_memory()
        dest = None if to_memory else Path(self.dest.text().strip()).expanduser()
        self.runner.start(core.decode_path, self.image, dest, force=self.overwrite.isChecked(),
                          text_in_memory=to_memory)
        self.status.emit("Decoding…")
        self.refresh()

    @staticmethod
    def _repair_note(m) -> str:
        if m.ecc and m.ecc.repaired_bytes:
            return f" Error correction repaired {m.ecc.repaired_bytes:,} damaged bytes."
        return ""

    def _finished(self, r) -> None:
        super()._finished(r)
        m = r.meta
        self.result_grid.clear()
        self._last_out = r.out
        if r.out is None:
            self.banner.show_message("ok", "Text decoded and verified",
                                     f"{m.orig_size:,} bytes; the SHA-256 matches the one stored at encode time."
                                     + self._repair_note(m))
            self.text_view.setPlainText(r.text or "")
            self.result_grid.add("SHA-256", m.digest.hex(), mono=True)
        else:
            self.banner.show_message("ok", "Restored and verified",
                                     "The SHA-256 matched before anything was written." + self._repair_note(m))
            self.result_grid.add("Restored to", str(r.out) + ("/" if m.is_dir else ""))
            self.result_grid.add("Size", f"{core.human(m.orig_size)} ({m.orig_size:,} bytes)"
                                 + (f" · {r.files:,} files" if m.is_dir else ""))
            self.result_grid.add("SHA-256", m.digest.hex(), mono=True)
            if m.ecc:
                text, tone = core.ecc_summary(m.ecc)
                self.result_grid.add("Error correction", text, tone=tone)
            self.result_grid.add("Integrity", "✓ verified", tone="success")
        self.text_view.setVisible(r.out is None)
        self.copy_btn.setVisible(r.out is None)
        self.reveal_btn.setVisible(r.out is not None)
        self.result_card.show()
        self.reveal_widget(self.result_card)
        self.status.emit("Decoded " + (r.out.name if r.out else "text"))
        self.refresh()


# -------------------------------------------------------------------------- inspect


class InspectPage(Page):
    title_text = "Inspect"
    subtitle_text = "Read an ImageStore PNG's metadata and verify its whole payload. Nothing is written."
    decodeRequested = pyqtSignal(Path)

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(settings, parent)
        self.image: Path | None = None
        self.drop = DropZone("verify", "Drop a PNG to inspect", "Checks the header, decodes the payload in memory "
                             "and compares its SHA-256.")
        self.drop.accepts = lambda p: p.is_file()
        self.drop.add_button(button("Choose image…", "image")).clicked.connect(self.choose)
        self.drop.dropped.connect(lambda paths: self.inspect(paths[0]))
        self.body.addWidget(self.drop)
        self.source_card = SourceCard()
        self.source_card.change.clicked.connect(self.choose)
        self.source_card.cleared.connect(self._clear)
        self.source_card.hide()
        self.body.addWidget(self.source_card)
        self.body.addWidget(self.progress)
        self.body.addWidget(self.banner)
        self.details, lay = card()
        lay.addWidget(label("Archive", "section"))
        self.grid = KeyValueGrid()
        lay.addWidget(self.grid)
        acts = QHBoxLayout()
        self.decode_btn = button("Decode this image…", "decode")
        self.decode_btn.clicked.connect(lambda: self.image and self.decodeRequested.emit(self.image))
        self.again = button("Check again", "verify")
        self.again.clicked.connect(self.primary)
        acts.addWidget(self.decode_btn)
        acts.addWidget(self.again)
        acts.addStretch(1)
        lay.addLayout(acts)
        self.details.hide()
        self.body.addWidget(self.details)
        self.finish_layout()

    def _clear(self) -> None:
        if self.busy:
            return
        self.image = None
        self.drop.show()
        self.source_card.hide()
        self.details.hide()
        self.banner.hide()

    def inspect(self, path: Path) -> None:
        if self.busy:
            return
        self.image = Path(path).resolve()
        self.drop.hide()
        self.source_card.show()
        self.source_card.show_source("image", self.image.name, str(self.image))
        self.primary()

    def primary(self) -> None:
        if self.image is None or self.busy:
            return
        self.banner.hide()
        self.details.hide()
        self.progress.begin("Reading image…")
        self.runner.start(core.inspect_path, self.image)
        self.status.emit("Inspecting…")

    def choose(self) -> None:
        start = str(self.image.parent if self.image else self.settings.value("last_dir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(self, "Choose a PNG to inspect", start,
                                              "PNG images (*.png);;All files (*)")
        if path:
            self.inspect(Path(path))

    def handle_paths(self, paths: list[Path]) -> None:
        self.inspect(paths[0])

    def _finished(self, r) -> None:
        super()._finished(r)
        g = self.grid
        g.clear()
        m = r.meta
        if r.ok:
            self.banner.show_message("ok", "Integrity verified",
                                     "The payload decodes completely and its SHA-256 matches the stored checksum.")
        elif m is None:
            ex = core.explain(r.error)
            self.banner.show_message("error", ex.title, ex.hint, ex.detail)
        else:
            ex = core.explain(r.error)
            self.banner.show_message("error", ex.title, ex.hint, ex.detail)
        g.add("File", f"{r.src}")
        g.add("PNG", f"{r.width} × {r.height} px, {r.mode}, {core.human(r.png_size)} on disk")
        if m is not None:
            used = core.HEADER.size + len(m.name.encode("utf-8")) + m.stored_size
            g.add("Contents", "text string" if m.is_text else f"{'folder' if m.is_dir else 'file'} “{m.name}”")
            g.add("Original size", f"{core.human(m.orig_size)} ({m.orig_size:,} bytes)")
            stored = f"{core.human(m.stored_size)} ({m.stored_size:,} bytes)"
            if (m.compressed or m.squeezed) and m.orig_size:
                stored += f" · {m.method}, {1 - m.stored_size / m.orig_size:.1%} smaller"
            else:
                stored += " · raw"
            g.add("Stored payload", stored)
            if r.capacity:
                parity = m.ecc.blocks * m.ecc.parity if m.ecc else 0
                g.add("Capacity", f"{core.human(r.capacity)} in {r.width * r.height:,} pixels · "
                                  f"{used / r.capacity:.1%} used, "
                                  + (f"{parity / r.capacity:.1%} error correction, " if parity else "")
                                  + f"{core.human(r.capacity - used - parity)} padding")
            if m.ecc:
                text, tone = core.ecc_summary(m.ecc)
                g.add("Error correction", text, tone=tone or None)
            flags = [n for f, n in ((core.FLAG_ZLIB, "zlib"), (core.FLAG_LZMA, "lzma"), (core.FLAG_TEXT, "text"), (core.FLAG_DIR, "folder"),
                                      (core.FLAG_SQUEEZE, "squeezed"), (core.FLAG_ECC, "ecc"))
                     if m.flags & f]
            g.add("Format", f"ImageStore v{core.VERSION} · flags 0x{m.flags:02x}"
                            + (f" ({', '.join(flags)})" if flags else ""))
            g.add("SHA-256 (stored)", m.digest.hex(), mono=True)
            if r.computed_digest is not None:
                match = r.computed_digest == m.digest
                g.add("SHA-256 (computed)", r.computed_digest.hex(), mono=True, tone="success" if match else "danger")
            g.add("Integrity", "✓ OK" if r.ok else "✗ FAILED", tone="success" if r.ok else "danger")
        self.decode_btn.setVisible(r.ok)
        self.details.show()
        self.status.emit("Integrity OK" if r.ok else "Integrity check failed")


# --------------------------------------------------------------------------- window


class MainWindow(QMainWindow):
    PAGES = (("encode", "Encode", "encode"), ("decode", "Decode", "decode"), ("inspect", "Inspect", "verify"))

    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self.settings = settings or QSettings("ImageStore", "ImageStore")
        self.setWindowTitle("ImageStore")
        self.setWindowIcon(style.app_icon("logo-imagestore"))
        self.resize(1080, 780)
        self.setMinimumSize(720, 520)
        self.setAcceptDrops(True)

        central = QWidget()
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._sidebar())
        self.stack = QStackedWidget()
        self.encode_page = EncodePage(self.settings)
        self.decode_page = DecodePage(self.settings)
        self.inspect_page = InspectPage(self.settings)
        self.pages = {"encode": self.encode_page, "decode": self.decode_page, "inspect": self.inspect_page}
        for page in self.pages.values():
            self.stack.addWidget(page)
            page.status.connect(lambda text: self.statusBar().showMessage(text, 8000))
        row.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.encode_page.inspectRequested.connect(self.inspect_file)
        self.inspect_page.decodeRequested.connect(self.decode_file)
        self._build_menus()
        format_label = QLabel(f"format v{core.VERSION} · RGBA · SHA-256")
        self.statusBar().addPermanentWidget(format_label)
        self.statusBar().showMessage(core.TAGLINE)
        self.go("encode")

    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(208)
        side.setStyleSheet(f"#Sidebar {{ background: {style.tokens()['window']}; "
                           f"border-right: 1px solid {style.tokens()['border']}; }}")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(12, 16, 12, 12)
        lay.setSpacing(4)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(style.app_icon("logo-imagestore").pixmap(QSize(28, 28)))
        brand.addWidget(logo)
        name = label("ImageStore", "title")
        brand.addWidget(name, 1)
        lay.addLayout(brand)
        tagline = label(core.TAGLINE, "faint", wrap=True)
        lay.addWidget(tagline)
        lay.addSpacing(14)
        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setAccessibleName("Sections")
        for key, text, icon in self.PAGES:
            item = QListWidgetItem(style.icon(icon), text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
        self.nav.setFixedHeight(len(self.PAGES) * 42 + 6)
        self.nav.currentRowChanged.connect(lambda i: self.stack.setCurrentIndex(i))
        lay.addWidget(self.nav)
        lay.addStretch(1)
        lay.addWidget(label(style.MOTTO, "muted"))
        lay.addWidget(label(style.MOTTO_FOOTNOTE, "faint"))
        return side

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        act = QAction(style.icon("open"), "&Choose Input…", self)
        act.setShortcut(QKeySequence.StandardKey.Open)
        act.triggered.connect(lambda: self.current_page().choose())
        m.addAction(act)
        act = QAction(style.icon("arrow-right"), "Run", self)
        act.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
        act.triggered.connect(lambda: self.current_page().primary())
        m.addAction(act)
        act = QAction(style.icon("stop"), "Cancel Operation", self)
        act.setShortcut(QKeySequence("Esc"))
        act.triggered.connect(lambda: self.current_page().cancel())
        m.addAction(act)
        m.addSeparator()
        act = QAction("&Quit", self)
        act.setShortcut(QKeySequence.StandardKey.Quit)
        act.setMenuRole(QAction.MenuRole.QuitRole)
        act.triggered.connect(self.close)
        m.addAction(act)

        m = mb.addMenu("&View")
        for i, (key, text, icon) in enumerate(self.PAGES, 1):
            act = QAction(style.icon(icon), text, self)
            act.setShortcut(QKeySequence(f"Ctrl+{i}"))
            act.triggered.connect(lambda _=False, k=key: self.go(k))
            m.addAction(act)
        m.addSeparator()
        theme = m.addMenu("Theme")
        group = QActionGroup(self)
        for key, text in (("dark", "Dark"), ("light", "Light")):
            act = QAction(text, self, checkable=True)
            act.setChecked(style.theme_name() == key)
            act.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            group.addAction(act)
            theme.addAction(act)

        m = mb.addMenu("&Help")
        act = QAction("Command-Line Usage", self)
        act.triggered.connect(self.show_cli_help)
        m.addAction(act)
        act = QAction("About ImageStore", self)
        act.setMenuRole(QAction.MenuRole.AboutRole)
        act.triggered.connect(self.about)
        m.addAction(act)

    def set_theme(self, name: str) -> None:
        from PyQt6.QtWidgets import QApplication

        self.settings.setValue("theme", name)
        style.apply_theme(QApplication.instance(), name)
        self.centralWidget().layout().itemAt(0).widget().setStyleSheet(
            f"#Sidebar {{ background: {style.tokens()['window']}; border-right: 1px solid {style.tokens()['border']}; }}")
        self.update()

    def current_page(self) -> Page:
        return self.stack.currentWidget()

    def go(self, key: str) -> None:
        index = list(self.pages).index(key)
        self.nav.setCurrentRow(index)
        self.stack.setCurrentIndex(index)

    def inspect_file(self, path: Path) -> None:
        self.go("inspect")
        self.inspect_page.inspect(path)

    def decode_file(self, path: Path) -> None:
        self.go("decode")
        self.decode_page.set_image(path)

    def show_cli_help(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Command-line usage")
        box.setText("ImageStore also runs from a terminal, without this window:")
        box.setInformativeText(core.build_parser().format_help())
        box.setStyleSheet(f"QLabel {{ font-family: '{style.mono_font().family()}'; }}")
        box.exec()

    def about(self) -> None:
        QMessageBox.about(
            self, "About ImageStore",
            f"<h3>ImageStore</h3><p>{core.TAGLINE}</p>"
            "<p>Files, folders and text are stored in all four channels of an RGBA PNG, with a SHA-256 "
            "digest so a decode either returns exactly what went in or says it was altered.</p>"
            f"<p>Format version {core.VERSION}.</p><p><b>{style.MOTTO}</b><br><i>{style.MOTTO_FOOTNOTE}</i></p>")

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.current_page().handle_paths(paths)

    def busy_pages(self) -> list[Page]:
        return [p for p in self.pages.values() if p.busy]

    def closeEvent(self, event) -> None:
        busy = self.busy_pages()
        if busy:
            answer = QMessageBox.question(self, "Operation running",
                                          "An operation is still running. Cancel it and quit? "
                                          "Partial output is removed.")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for page in busy:
                page.cancel()
            for page in busy:
                page.runner.wait(10)
        event.accept()
