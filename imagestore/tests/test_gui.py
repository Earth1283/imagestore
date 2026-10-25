"""Headless tests of the optional GUI (Qt offscreen platform). Skipped without PyQt6.

The GUI is a frontend over the same operations the CLI uses; these tests drive the pages
the way a user would and check what ends up on disk.
"""
from __future__ import annotations

import os
import random
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QSettings  # noqa: E402

import imagestore  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    from imagestore_gui.window import MainWindow

    w = MainWindow(QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat))
    w.resize(1100, 800)
    w.show()
    yield w
    for page in w.pages.values():
        page.cancel()
        page.runner.wait(10)
    w.close()


def wait(app, page, timeout=20.0):
    end = time.time() + timeout
    runners = [page.runner] + [getattr(page, n) for n in ("scanner", "peeker") if hasattr(page, n)]
    while time.time() < end:
        app.processEvents()
        if not any(r.busy for r in runners):
            # deliver the queued finished/failed signal
            for _ in range(5):
                app.processEvents()
                time.sleep(0.005)
            return
        time.sleep(0.005)
    raise AssertionError("operation did not finish")


def test_gui_is_not_imported_by_the_cli():
    """The CLI must work without PyQt6: nothing GUI-related is imported until -g/--gui or 'g'."""
    import subprocess
    import sys

    code = ("import sys; sys.path.insert(0, %r); import imagestore; "
            "print(any(m.startswith(('PyQt6', 'imagestore_gui')) for m in sys.modules))") % str(
        Path(imagestore.__file__).parent)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False"


def test_encode_file_page_reports_stats_and_writes_verified_png(app, window, tmp_path):
    src = tmp_path / "notes.txt"
    data = b"ImageStore GUI test\n" * 5000
    src.write_bytes(data)
    page = window.encode_page
    page.handle_paths([src])
    assert page.output.text() == str(tmp_path / "notes.txt.png")
    assert page.go.isEnabled()
    page.primary()
    wait(app, page)
    out = tmp_path / "notes.txt.png"
    assert out.exists()
    assert page.result_card.isVisibleTo(window)
    assert page.banner.title.text() == "Encoded and verified"
    assert page.tiles["orig"].value.text() == imagestore.human(len(data))
    assert page.tiles["ratio"].value.text().endswith("%")
    assert page.tiles["check"].value.text() == "✓ Match"
    res = imagestore.inspect_path(out)
    assert res.ok and page.sha_value.text() == res.meta.digest.hex()
    # the output now exists: running again needs Overwrite
    page.refresh()
    assert not page.go.isEnabled()
    page.overwrite.setChecked(True)
    assert page.go.isEnabled()


def test_encode_text_page(app, window, tmp_path):
    page = window.encode_page
    page.kind.set_value("text")
    page.text.setPlainText("Code is Art.* ✓")
    page.output.setText(str(tmp_path / "msg.png"))
    page._output_edited("")
    page.primary()
    wait(app, page)
    result = imagestore.decode_path(tmp_path / "msg.png")
    assert result.text == "Code is Art.* ✓"


def test_encode_folder_with_reviewed_auto_ignore(app, window, tmp_path):
    proj = tmp_path / "proj"
    for rel, body in {"a.txt": b"a", "node_modules/m.js": b"m", ".DS_Store": b"d", "__pycache__/c.pyc": b"c"}.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_bytes(body)
    page = window.encode_page
    page.set_source(proj)
    wait(app, page)
    assert page.ignore_card.isVisibleTo(window)
    assert page.excluded() == {"node_modules", ".DS_Store", "__pycache__"}
    # the user decides to keep node_modules
    tree = page.ignore_tree
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        if item.text(0) == "node_modules/":
            item.setCheckState(0, item.checkState(0).__class__.Unchecked)
    assert page.excluded() == {".DS_Store", "__pycache__"}
    assert not page.ignore_all.isChecked()
    page.primary()
    wait(app, page)
    dest = tmp_path / "restored"
    imagestore.decode_path(tmp_path / "proj.png", dest)
    files = {p.relative_to(dest / "proj").as_posix() for p in (dest / "proj").rglob("*") if p.is_file()}
    assert files == {"a.txt", "node_modules/m.js"}


def test_encode_cancel_cleans_up(app, window, tmp_path):
    src = tmp_path / "big.bin"
    src.write_bytes(random.Random(3).randbytes(24 << 20))
    page = window.encode_page
    page.set_source(src)
    page.compress.setChecked(False)
    page.primary()
    end = time.time() + 10
    while time.time() < end and not page.runner._last[0]:
        app.processEvents()
        time.sleep(0.002)
    page.cancel()
    wait(app, page)
    assert page.banner.title.text() == "Cancelled"
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.endswith(".ini")) == ["big.bin"]
    page.compress.setChecked(True)


def test_decode_page_restores_and_refuses_overwrite(app, window, tmp_path):
    src = tmp_path / "data.bin"
    data = random.Random(4).randbytes(5000)
    src.write_bytes(data)
    png = tmp_path / "data.png"
    imagestore.encode_path(src, png)
    page = window.decode_page
    page.handle_paths([png])
    wait(app, page)
    assert "data.bin" in page.source_card.detail.full_text()
    dest = tmp_path / "out"
    page.dest.setText(str(dest))
    page.refresh()
    assert page.go.isEnabled()
    page.primary()
    wait(app, page)
    assert (dest / "data.bin").read_bytes() == data
    assert page.banner.title.text() == "Restored and verified"
    page.refresh()
    assert not page.go.isEnabled()  # exists now; needs Overwrite
    assert "already exists" in page.dest_note.text()


def test_decode_page_text_shown_in_memory(app, window, tmp_path):
    png = tmp_path / "t.png"
    imagestore.encode_text("hello there", png)
    window.decode_file(png)
    page = window.decode_page
    wait(app, page)
    assert page.text_options.isVisibleTo(window)
    page.primary()
    wait(app, page)
    assert page.text_view.toPlainText() == "hello there"
    assert not (tmp_path / "text.txt").exists()


def test_inspect_page_flags_tampering_and_foreign_png(app, window, tmp_path):
    from PIL import Image

    src = tmp_path / "x.bin"
    src.write_bytes(random.Random(5).randbytes(4000))
    png = tmp_path / "x.png"
    imagestore.encode_path(src, png, compress=False)
    page = window.inspect_page
    window.inspect_file(png)
    wait(app, page)
    assert page.banner.title.text() == "Integrity verified"
    im = Image.open(png)
    raw = bytearray(im.tobytes())
    raw[500] ^= 1
    bad = tmp_path / "bad.png"
    Image.frombytes("RGBA", im.size, bytes(raw)).save(bad)
    window.inspect_file(bad)
    wait(app, page)
    assert page.banner.title.text() == "Integrity check failed (SHA-256 mismatch)"
    assert "SHA-256 (computed)" in page.grid.values
    assert not page.decode_btn.isVisibleTo(window)
    foreign = tmp_path / "photo.png"
    Image.new("RGBA", (40, 40), (10, 20, 30, 255)).save(foreign)
    window.inspect_file(foreign)
    wait(app, page)
    assert page.banner.title.text() == "Not an ImageStore image"


def test_window_routes_drops_and_shortcuts(app, window, tmp_path):
    png = tmp_path / "r.png"
    imagestore.encode_text("route", png)
    window.go("inspect")
    window.current_page().handle_paths([png])
    wait(app, window.inspect_page)
    assert window.inspect_page.image == png.resolve()
    window.inspect_page.decodeRequested.emit(png)
    assert window.current_page() is window.decode_page
    wait(app, window.decode_page)
    assert window.decode_page.meta is not None and window.decode_page.meta.is_text


def test_close_while_running_cancels_and_waits(app, tmp_path, monkeypatch):
    from imagestore_gui.window import MainWindow

    w = MainWindow(QSettings(str(tmp_path / "c.ini"), QSettings.Format.IniFormat))
    w.show()
    src = tmp_path / "big.bin"
    src.write_bytes(random.Random(6).randbytes(24 << 20))
    page = w.encode_page
    page.set_source(src)
    page.compress.setChecked(False)
    page.primary()
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    w.close()
    assert not page.runner.busy
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".") or p.suffix == ".png"]
    assert leftovers == [] or all(not n.endswith(".part") for n in leftovers)
    page.compress.setChecked(True)
