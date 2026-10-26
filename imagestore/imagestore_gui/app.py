"""QApplication setup for the ImageStore window."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

_here = str(Path(__file__).resolve().parent.parent)
if _here not in sys.path:  # `python -m imagestore_gui` from elsewhere: find imagestore.py
    sys.path.insert(0, _here)

from PyQt6.QtCore import QSettings  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from . import style  # noqa: E402


def _install_excepthook() -> None:
    """PyQt6 aborts on an unhandled exception in a slot unless an excepthook is set; show it
    instead (the operation that raised has already cleaned up its partial output)."""

    def hook(exc_type, exc, tb):
        print("".join(traceback.format_exception(exc_type, exc, tb)), file=sys.stderr)
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "ImageStore internal error",
                                 f"An unexpected error occurred.\n\n{exc_type.__name__}: {exc}")

    sys.excepthook = hook


def run(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(list(argv or sys.argv[:1]))
    app.setApplicationName("ImageStore")
    app.setOrganizationName("ImageStore")
    _install_excepthook()
    settings = QSettings("ImageStore", "ImageStore")
    style.apply_theme(app, str(settings.value("theme", "dark")))
    from .window import MainWindow

    window = MainWindow(settings)
    window.show()
    window.raise_()
    return app.exec()
