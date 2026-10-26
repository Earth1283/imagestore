"""Measure time and peak RSS for large-image operations and GUI responsiveness.

    python scripts/benchmark.py [--size 7488x7487] [--workdir DIR] [--json FILE]

Each scenario runs in its own subprocess so ``ru_maxrss`` reflects only that scenario.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1e3  # bytes on macOS, KiB on Linux


def scenario(name: str, path: str, out: str) -> dict:
    import numpy as np

    from exact.core import transforms as T
    from exact.core.document import Document, Rect

    t = {}
    start = time.perf_counter()
    if name == "create":
        from exact.core.png_io import save_png

        w, h = (int(v) for v in out.split("x"))
        arr = np.random.default_rng(921).integers(0, 256, (h, w, 4), dtype=np.uint8)
        t["generate"] = time.perf_counter() - start
        s = time.perf_counter()
        save_png(path, arr)
        t["verified_save"] = time.perf_counter() - s
        return {"times": t, "peak_rss_mb": peak_rss_mb()}

    doc = Document.open(path)
    t["open"] = time.perf_counter() - start
    if name == "open":
        pass
    elif name == "rotate":
        s = time.perf_counter()
        doc.transform(T.ROTATE_CW)
        t["rotate_cw"] = time.perf_counter() - s
        s = time.perf_counter()
        doc.transform(T.ROTATE_CCW)
        t["rotate_ccw"] = time.perf_counter() - s
        assert doc.current_sha256() == doc.original_sha256
    elif name == "edit_save":
        s = time.perf_counter()
        doc.begin_stroke((255, 0, 0, 255), size=4)
        for i in range(0, 4000, 7):
            doc.stroke_to(i, i // 2)
        summary = doc.end_stroke()
        t["pencil_stroke"] = time.perf_counter() - s
        s = time.perf_counter()
        doc.fill_rect(Rect(100, 100, 512, 512), (0, 0, 0, 0))
        t["fill_512"] = time.perf_counter() - s
        s = time.perf_counter()
        doc.current_sha256()
        t["sha256"] = time.perf_counter() - s
        s = time.perf_counter()
        doc.save(dest=out)
        t["verified_save_full"] = time.perf_counter() - s
        doc.set_pixel(min(1234, doc.width - 1), min(4321, doc.height - 1), (1, 2, 3, 4))
        s = time.perf_counter()
        report = doc.save()
        t["verified_save_after_1px_edit"] = time.perf_counter() - s
        t["bands_reused"] = f"{report.bands_reused}/{report.bands_total}"
        t["stroke_changed_pixels"] = summary.changed_pixels
    elif name == "gui":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        from exact.gui.canvas import DocumentSource

        app = QApplication.instance() or QApplication([])
        src = DocumentSource(doc)
        src.highlight_changes = True
        s = time.perf_counter()
        for _ in range(20):
            src.render(3000, 3000, 3000 + 1600, 3000 + 1000, 1, 1.0)
        t["render_1600x1000_viewport_ms"] = (time.perf_counter() - s) / 20 * 1000
        s = time.perf_counter()
        for _ in range(20):
            src.render(0, 0, doc.width, doc.height, 8, 0.125)
        t["render_whole_image_at_12.5%_ms"] = (time.perf_counter() - s) / 20 * 1000
        from exact.gui.main_window import MainWindow
        from PyQt6.QtCore import QSettings

        win = MainWindow(QSettings(os.path.join(os.path.dirname(out), "bench.ini"), QSettings.Format.IniFormat))
        win.resize(1600, 1000)
        win.show()
        win.set_document(doc)
        win.canvas.set_zoom(1)
        win.canvas.center_on(3700, 3700)
        s = time.perf_counter()
        win.canvas.viewport().grab()
        t["paint_cold_ms"] = (time.perf_counter() - s) * 1000
        s = time.perf_counter()
        win.canvas.viewport().grab()
        t["paint_cached_ms"] = (time.perf_counter() - s) * 1000
        win.canvas.horizontalScrollBar().setValue(win.canvas.horizontalScrollBar().value() + 64)
        s = time.perf_counter()
        win.canvas.viewport().grab()
        t["paint_after_64px_scroll_ms"] = (time.perf_counter() - s) * 1000
        win.close()
        del app
    elif name == "ui":
        t.update(_ui_scenario(doc, out))
    return {"times": t, "peak_rss_mb": peak_rss_mb()}


def _ui_scenario(doc, out: str) -> dict:
    """Responsiveness of the redesigned workspace: what one user interaction costs on the GUI
    thread. Each figure is the mean of repeated runs, in milliseconds."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QEvent, QPointF, QSettings, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication

    from exact.gui import style
    from exact.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    style.apply_theme(app, "dark")
    t: dict = {}

    def ms(fn, n=20) -> float:
        fn()  # warm-up
        s = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - s) / n * 1000

    def pump(seconds=0.0):
        end = time.perf_counter() + seconds
        while True:
            app.processEvents()
            if time.perf_counter() >= end:
                break

    win = MainWindow(QSettings(os.path.join(os.path.dirname(out), "bench-ui.ini"), QSettings.Format.IniFormat))
    win.resize(1600, 1000)
    win.show()
    s = time.perf_counter()
    win.set_document(doc)
    t["set_document_ms"] = (time.perf_counter() - s) * 1000
    cx, cy = doc.width // 2, doc.height // 2
    canvas = win.canvas
    canvas.set_zoom(1)
    canvas.center_on(cx, cy)
    s = time.perf_counter()
    canvas.viewport().grab()
    t["paint_cold_ms"] = (time.perf_counter() - s) * 1000
    t["paint_cached_ms"] = ms(lambda: canvas.viewport().grab())
    pos = [0]

    def scroll():
        pos[0] += 64
        canvas.horizontalScrollBar().setValue(canvas.horizontalScrollBar().value() + (64 if pos[0] % 128 else -64))
        canvas.viewport().grab()
    t["scroll_64px_repaint_ms"] = ms(scroll)
    zooms = iter([2, 1] * 50)
    t["zoom_step_repaint_ms"] = ms(lambda: (canvas.set_zoom(next(zooms)), canvas.viewport().grab()), 10)
    canvas.set_zoom(1)
    canvas.center_on(cx, cy)
    k = iter(range(10_000))
    t["hover_update_ms"] = ms(lambda: win._hover_changed((cx + next(k) % 50, cy)), 200)
    t["inspect_pixel_ms"] = ms(lambda: win.set_inspected(cx + next(k) % 50, cy), 100)
    win.set_workspace("inspect")
    pump()
    t["hex_paint_ms"] = ms(lambda: win.hex_panel.view.viewport().grab(), 20)
    t["hex_jump_to_end_ms"] = ms(lambda: win._goto_offset(doc.pixels.size - 1 - next(k)), 20)
    win.set_workspace("edit")
    t["workspace_switch_ms"] = ms(lambda: (win.set_workspace("inspect"), win.set_workspace("edit"), pump()), 5) / 2

    # pencil: mouse events through the canvas, as a user drags
    win.set_tool("pencil")
    canvas.set_zoom(4)
    canvas.center_on(cx, cy)
    calls = []
    start = win.worker.start
    win.worker.start = lambda d: calls.append(1) or start(d)

    def ev(kind, x, y):
        p = canvas.image_to_view(x + 0.5, y + 0.5)
        btn = Qt.MouseButton.LeftButton
        e = QMouseEvent(kind, QPointF(p), QPointF(p), btn,
                        btn if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.MouseButtonPress: canvas.mousePressEvent, QEvent.Type.MouseMove: canvas.mouseMoveEvent,
         QEvent.Type.MouseButtonRelease: canvas.mouseReleaseEvent}[kind](e)

    ev(QEvent.Type.MouseButtonPress, cx - 100, cy)
    moves = []
    for i in range(200):
        s = time.perf_counter()
        ev(QEvent.Type.MouseMove, cx - 100 + i, cy + (i % 7))
        canvas.viewport().repaint()
        moves.append(time.perf_counter() - s)
    s = time.perf_counter()
    ev(QEvent.Type.MouseButtonRelease, cx + 100, cy)
    t["stroke_move_repaint_ms_mean"] = sum(moves) / len(moves) * 1000
    t["stroke_move_repaint_ms_max"] = max(moves) * 1000
    t["stroke_release_ms"] = (time.perf_counter() - s) * 1000
    t["hashes_started_during_stroke"] = len(calls)
    s = time.perf_counter()
    while win.hashes is None and time.perf_counter() - s < 30:
        pump(0.005)
    t["release_to_sha256_confirmed_ms"] = (time.perf_counter() - s) * 1000  # includes the 500 ms debounce
    t["one_pixel_edit_ui_ms"] = ms(lambda: win._apply_pixel(cx, cy, (next(k) % 256, 1, 2, 3)), 20)
    for i in range(500):
        doc.set_pixel(i % doc.width, 0, (i % 256, 0, 0, 0))
    t["document_changed_500_steps_ms"] = ms(lambda: win.document_changed(None), 10)
    t["history_steps"] = len(doc.history)
    doc.saved_state = doc.history.state_id
    win.close()
    return t


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="7488x7487")
    parser.add_argument("--workdir")
    parser.add_argument("--_run", nargs=3, metavar=("NAME", "PATH", "OUT"))
    parser.add_argument("--json", metavar="FILE", help="also write the raw results as JSON")
    args = parser.parse_args()
    if args._run:
        print(json.dumps(scenario(*args._run)))
        return 0
    workdir = args.workdir or tempfile.mkdtemp(prefix="exact-bench-")
    src = os.path.join(workdir, "bench.png")
    out = os.path.join(workdir, "bench-out.png")
    w, h = (int(v) for v in args.size.split("x"))
    results: dict = {}
    print(f"eXact benchmark: {w}x{h} RGBA random payload ({w * h * 4 / 1e6:.0f} MB raw), python {sys.version.split()[0]}")
    for name, extra in (("create", args.size), ("open", out), ("rotate", out), ("edit_save", out), ("gui", out),
                        ("ui", out)):
        proc = subprocess.run([sys.executable, __file__, "--_run", name, src, extra],
                              capture_output=True, text=True)
        if proc.returncode:
            print(proc.stderr)
            return 1
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        times = ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in result["times"].items())
        if args.json:
            results[name] = result
        print(f"{name:10s} peak RSS {result['peak_rss_mb']:7.0f} MB | {times}")
    print(f"PNG size: {os.path.getsize(src) / 1e6:.1f} MB  (workdir {workdir})")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
