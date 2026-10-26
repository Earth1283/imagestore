"""Run slow work (decode, encode+verify, full-image transforms) off the GUI thread.

NumPy, zlib, hashlib and Pillow release the GIL for bulk work, so the UI stays live.
The caller blocks in a nested event loop behind an application-modal progress dialog,
which keeps the user from mutating the buffer while a worker reads it.

Read-only work (opening a file, comparing files) may be cancelled: the caller stops
waiting at once and the worker's eventual result is discarded, so no state changes. Work
that writes (saving, transforms) is never cancellable: its atomic replace must complete.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QProgressDialog


class TaskCancelled(Exception):
    """The user cancelled a read-only task; nothing was changed."""


def run_blocking(parent, label: str, fn, *args, cancellable: bool = False, **kwargs):
    outcome: dict = {}

    def worker():
        try:
            outcome["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # re-raised on the GUI thread
            outcome["error"] = exc

    dialog = QProgressDialog(label, "Cancel" if cancellable else None, 0, 0, parent)
    dialog.setWindowTitle("eXact")
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setMinimumDuration(250)
    if not cancellable:
        dialog.setCancelButton(None)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    if parent is not None and hasattr(parent, "set_busy"):
        parent.set_busy(True)
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    thread = threading.Thread(target=worker, name="exact-task", daemon=True)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(20)
    cancelled = False

    def poll():
        nonlocal cancelled
        if cancellable and dialog.wasCanceled():
            cancelled = True
            loop.quit()
        elif not thread.is_alive():
            loop.quit()

    timer.timeout.connect(poll)
    try:
        thread.start()
        timer.start()
        if thread.is_alive():
            loop.exec()
        if not cancelled:
            thread.join()
    finally:
        timer.stop()
        dialog.close()
        dialog.deleteLater()
        QApplication.restoreOverrideCursor()
        if parent is not None and hasattr(parent, "set_busy"):
            parent.set_busy(False)
    if cancelled:
        raise TaskCancelled(label)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")
