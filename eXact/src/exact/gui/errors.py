"""Turn exceptions into actionable messages, keeping the technical detail one click away."""

from __future__ import annotations

import errno
import traceback
from dataclasses import dataclass

from PyQt6.QtWidgets import QMessageBox

from ..core.diff import ComparisonMismatch
from ..core.png_chunks import PngFormatError
from ..core.png_io import SaveVerificationError, UnsupportedPngError


@dataclass
class Explanation:
    title: str
    message: str
    hint: str = ""


def _os_explanation(exc: OSError, action: str) -> Explanation:
    where = exc.filename or "the file"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return Explanation("Permission denied", f"eXact is not allowed to {action} {where}.",
                           "Check the file's permissions, or choose another location.")
    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return Explanation("Not enough disk space", f"The disk filled up while trying to {action} {where}.",
                           "Free some space or save somewhere else. The temporary file was removed.")
    if exc.errno == errno.ENOENT:
        return Explanation("File not found", f"{where} does not exist.", "It may have been moved, renamed or deleted.")
    if exc.errno == errno.EROFS:
        return Explanation("Read-only location", f"{where} is on a read-only file system.",
                           "Save a copy somewhere writable with File > Save As.")
    if exc.errno == errno.EISDIR:
        return Explanation("That is a folder", f"{where} is a folder, not a PNG file.")
    return Explanation(f"Could not {action} the file", str(exc))


def explain(exc: BaseException, action: str = "open") -> Explanation:
    if isinstance(exc, UnsupportedPngError):
        return Explanation("Unsupported PNG format", str(exc),
                           "eXact edits only 8-bit RGB and 8-bit RGBA PNGs, and never converts an image, because "
                           "conversion would change channel values. Nothing was modified.")
    if isinstance(exc, PngFormatError):
        return Explanation("Not a valid PNG", f"The file could not be parsed: {exc}.",
                           "It may be truncated, damaged, or another format with a .png name. "
                           "Nothing was modified.")
    if isinstance(exc, SaveVerificationError):
        return Explanation("Save verification failed", f"The written file did not decode to exactly the "
                           f"intended pixels: {exc}.",
                           "The original file was NOT modified and the temporary file was removed. Your "
                           "unsaved edits are still in memory; try saving elsewhere.")
    if isinstance(exc, ComparisonMismatch):
        return Explanation("Images cannot be compared pixel for pixel", str(exc),
                           "A pixel comparison needs equal dimensions and the same mode (RGB or RGBA).")
    if isinstance(exc, MemoryError):
        return Explanation("Not enough memory", f"There was not enough memory to {action} this image.",
                           "eXact keeps the pixel buffer plus an untouched original copy (about twice the raw "
                           "size). Close other programs or documents and try again.")
    if isinstance(exc, OSError):
        return _os_explanation(exc, action)
    name = type(exc).__name__
    if name == "DecompressionBombError":
        return Explanation("Image too large", str(exc), "The decoder refused the image as unreasonably large.")
    return Explanation(f"Could not {action} the file", f"{name}: {exc}")


def show_error(parent, exc: BaseException, action: str = "open", *, note: str = "") -> None:
    """A warning box with a plain-language title and hint; the exception is under Show Details."""
    ex = explain(exc, action)
    box = QMessageBox(parent)
    critical = isinstance(exc, (SaveVerificationError, MemoryError))
    box.setIcon(QMessageBox.Icon.Critical if critical else QMessageBox.Icon.Warning)
    box.setWindowTitle(ex.title)
    box.setText(f"<b>{ex.title}</b>")
    box.setInformativeText("\n\n".join(t for t in (ex.message, ex.hint, note) if t))
    box.setDetailedText("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    box.exec()
