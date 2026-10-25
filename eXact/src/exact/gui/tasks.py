"""Run slow work (decode, encode+verify, full-image transforms) off the GUI thread.

NumPy, zlib, hashlib and Pillow release the GIL for bulk work, so the UI stays live.
The caller blocks in a nested event loop behind an application-modal progress dialog,
which keeps the user from mutating the buffer while a worker reads it.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QProgressDialog


def run_blocking(parent, label: str, fn, *args, **kwargs):
    outcome: dict = {}

    def worker():
        try:
            outcome["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # re-raised on the GUI thread
            outcome["error"] = exc

    dialog = QProgressDialog(label, None, 0, 0, parent)
    dialog.setWindowTitle("eXact")
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setMinimumDuration(250)
    dialog.setCancelButton(None)
    dialog.setAutoClose(False)
    if parent is not None and hasattr(parent, "set_busy"):
        parent.set_busy(True)
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    thread = threading.Thread(target=worker, name="exact-task", daemon=True)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: None if thread.is_alive() else loop.quit())
    try:
        thread.start()
        timer.start()
        if thread.is_alive():
            loop.exec()
        thread.join()
    finally:
        timer.stop()
        dialog.close()
        dialog.deleteLater()
        QApplication.restoreOverrideCursor()
        if parent is not None and hasattr(parent, "set_busy"):
            parent.set_busy(False)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")
