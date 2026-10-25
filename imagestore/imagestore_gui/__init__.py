"""Optional graphical interface for ImageStore (PyQt6).

Never started by default: open it with ``python imagestore.py -g`` (or ``--gui``), with
``g`` in the interactive menu, or with ``python -m imagestore_gui`` from this folder.
The command line works without PyQt6 installed.
"""

from __future__ import annotations


def run(argv: list[str] | None = None) -> int:
    from .app import run as _run

    return _run(argv)
