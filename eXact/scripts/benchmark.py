"""Measure time and peak RSS for large-image operations.

    python scripts/benchmark.py [--size 7488x7487] [--workdir DIR]

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
        doc.set_pixel(1234, 4321, (1, 2, 3, 4))
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
    return {"times": t, "peak_rss_mb": peak_rss_mb()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="7488x7487")
    parser.add_argument("--workdir")
    parser.add_argument("--_run", nargs=3, metavar=("NAME", "PATH", "OUT"))
    args = parser.parse_args()
    if args._run:
        print(json.dumps(scenario(*args._run)))
        return 0
    workdir = args.workdir or tempfile.mkdtemp(prefix="exact-bench-")
    src = os.path.join(workdir, "bench.png")
    out = os.path.join(workdir, "bench-out.png")
    w, h = (int(v) for v in args.size.split("x"))
    print(f"eXact benchmark: {w}x{h} RGBA random payload ({w * h * 4 / 1e6:.0f} MB raw), python {sys.version.split()[0]}")
    for name, extra in (("create", args.size), ("open", out), ("rotate", out), ("edit_save", out), ("gui", out)):
        proc = subprocess.run([sys.executable, __file__, "--_run", name, src, extra],
                              capture_output=True, text=True)
        if proc.returncode:
            print(proc.stderr)
            return 1
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        times = ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in result["times"].items())
        print(f"{name:10s} peak RSS {result['peak_rss_mb']:7.0f} MB | {times}")
    print(f"PNG size: {os.path.getsize(src) / 1e6:.1f} MB  (workdir {workdir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
