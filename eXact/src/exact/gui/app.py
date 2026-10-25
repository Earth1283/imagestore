"""GUI entry point: ``exact [FILE.png]`` or ``python -m exact [FILE.png]``."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("eXact's GUI needs PyQt6: pip install PyQt6", file=sys.stderr)
        return 1
    from .. import __version__
    from .main_window import MainWindow

    app = QApplication(argv)
    _install_excepthook()
    app.setApplicationName("eXact")
    app.setOrganizationName("eXact")
    app.setApplicationVersion(__version__)
    window = MainWindow()
    window.show()
    files = [a for a in argv[1:] if not a.startswith("-")]
    if files:
        window.open_path(files[0])
    return app.exec()


def _install_excepthook() -> None:
    """PyQt6 aborts the process on an unhandled exception in a Qt callback unless a custom
    excepthook is installed; report it instead so unsaved work is not lost."""
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        print(text, file=sys.stderr)
        from PyQt6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is not None:
            QMessageBox.critical(None, "eXact internal error",
                                 "An unexpected error occurred. The pixel buffer was not saved or altered "
                                 f"by this error handler.\n\n{exc_type.__name__}: {exc}")

    sys.excepthook = hook


if __name__ == "__main__":
    sys.exit(main())
