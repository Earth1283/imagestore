"""Power Tools: the slightly cursed corner of eXact.

eXact exists so that nothing ever changes a byte you did not mean to change. Power Tools
change a great many bytes, on purpose: a paint bucket and shapes that pick the pixels
for you, and filters that compute values no one typed. They are fenced off accordingly
(hazard tape in the toolbar, a menu of their own) and every one follows the same three
steps:

    Preview   the result is computed into a separate array and drawn over the canvas.
              The buffer is untouched; the preview costs nothing to throw away.
    Apply     the precomputed result is written as one undoable step, with the usual
              exact accounting (the integrity chips count every changed byte).
    Undo      takes back the last power-tool step from the same place.

Apply reuses the preview instead of computing it again, and heavy work runs off the GUI
thread behind the progress dialog.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import power
from ..core.document import Document, OperationSummary, Rect
from . import display, style
from .panels import PowerActions, chip, muted, section

LIVE_PIXELS = 2_000_000  # previews up to this size recompute live as you drag a slider; bigger
# targets preview just their visible part when that is small enough, otherwise on demand
MAGNIFIER_SPAN = 64  # image pixels per side in the dialog's before/after magnifier
MAGNIFIER_SCALE = 3


# ------------------------------------------------------------------------- pending


@dataclass(eq=False)
class Pending:
    """A computed power-tool result that has not been written to the buffer.

    Exactly one of *values* (the new rect), *mask* (pixels to paint *color* into) or
    *points* (sparse ``(ys, xs, values)``, sorted by row) is set.
    """

    label: str
    source: str  # "bucket", "shape" or a filter key
    rect: Rect  # bounding box, inside the image
    generation: int  # document generation it was computed from
    values: np.ndarray | None = None
    mask: np.ndarray | None = None
    color: tuple | None = None
    write_mask: tuple | None = None
    points: tuple | None = None
    settings: tuple = ()
    verb: str = "Apply"  # the name of the action that writes it, same on the button and the canvas
    partial: bool = False  # covers only the visible part of a large target (Apply recomputes it all)

    def __post_init__(self):
        if self.mask is not None:
            self.count = int(np.count_nonzero(self.mask))
        elif self.points is not None:
            self.count = len(self.points[0])
        else:
            self.count = self.rect.w * self.rect.h

    def describe(self) -> str:
        r = self.rect
        noun = "pixel" if self.count == 1 else "pixels"
        where = f"{r.w} × {r.h} at ({r.x}, {r.y})" + (" (visible area)" if self.partial else "")
        if self.values is not None:
            return f"up to {self.count:,} {noun} in {where}"
        return f"{self.count:,} {noun} in {where}"

    def is_current(self, doc: Document, settings: tuple) -> bool:
        return self.generation == doc.generation and self.settings == settings

    def overlay(self, cur: np.ndarray, x0: int, y0: int, step: int) -> None:
        """Draw the result into *cur*, a display copy of pixels[y0::step, x0::step]."""
        h, w = cur.shape[:2]
        if self.points is not None:
            ys, xs, vals = self.points
            lo, hi = np.searchsorted(ys, [y0, y0 + h * step])
            ys, xs, vals = ys[lo:hi], xs[lo:hi], vals[lo:hi]
            sel = (xs >= x0) & (xs < x0 + w * step)
            if step > 1:
                sel &= ((ys - y0) % step == 0) & ((xs - x0) % step == 0)
            cur[(ys[sel] - y0) // step, (xs[sel] - x0) // step] = vals[sel]
            return
        r = self.rect
        i0, i1 = max(0, -(-(r.x - x0) // step)), min(w, -(-(r.x1 - x0) // step))
        j0, j1 = max(0, -(-(r.y - y0) // step)), min(h, -(-(r.y1 - y0) // step))
        if i0 >= i1 or j0 >= j1:
            return
        sx, sy = x0 + i0 * step - r.x, y0 + j0 * step - r.y
        rows = slice(sy, sy + (j1 - j0 - 1) * step + 1, step)
        cols = slice(sx, sx + (i1 - i0 - 1) * step + 1, step)
        dst = cur[j0:j1, i0:i1]
        if self.values is not None:
            dst[...] = self.values[rows, cols]
            return
        m = self.mask[rows, cols]
        for c, on in enumerate(self.write_mask[:cur.shape[2]]):
            if on:
                dst[..., c][m] = self.color[c]

    def commit(self, doc: Document) -> OperationSummary:
        if self.values is not None:
            return doc.commit_region(self.rect, self.values, self.label)
        if self.mask is not None:
            return doc.paint_mask(self.rect.x, self.rect.y, self.mask, self.color, self.write_mask, self.label)
        ys, xs, vals = self.points
        return doc.write_pixels(ys, xs, vals, self.label)


def clip_mask(found, limit: Rect | None):
    """Restrict a ``(left, top, mask)`` result to *limit* (the selection)."""
    if found is None:
        return None
    left, top, mask = found
    if limit is None:
        return found
    r = Rect(left, top, mask.shape[1], mask.shape[0])
    x0, y0 = max(r.x, limit.x), max(r.y, limit.y)
    x1, y1 = min(r.x1, limit.x1), min(r.y1, limit.y1)
    if x0 >= x1 or y0 >= y1:
        return None
    mask = mask[y0 - top:y1 - top, x0 - left:x1 - left]
    if not mask.any():
        return None
    rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    return x0 + int(cols[0]), y0 + int(rows[0]), mask[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def mask_pending(found, *, label: str, source: str, verb: str, doc: Document, color, write_mask,
                 settings=()) -> Pending | None:
    if found is None:
        return None
    left, top, mask = found
    return Pending(label, source, Rect(left, top, mask.shape[1], mask.shape[0]), doc.generation, mask=mask,
                   color=tuple(color), write_mask=tuple(write_mask), settings=settings, verb=verb)


# ------------------------------------------------------------------------- filters


@dataclass(frozen=True)
class FilterSpec:
    key: str
    title: str
    icon: str
    verb: str
    tagline: str
    label: str  # history label prefix


FILTERS = {
    "blur": FilterSpec("blur", "Gaussian Blur", "blur", "Blur",
                       "Averages every pixel with its neighbours. The one thing eXact was built never to do.",
                       "Gaussian blur"),
    "invert": FilterSpec("invert", "Invert Colours", "invert", "Invert",
                         "255 − v in every ticked channel. Lossless and its own inverse, which makes it the least "
                         "cursed thing in this menu.", "Invert"),
    "nuke": FilterSpec("nuke", "Photo Nuker", "nuke", "Nuke it",
                       "Saturation, contrast, posterisation, JPEG-grade chroma blocks and grain. For memes. Not for "
                       "payloads. Never for payloads.", "Nuke"),
    "cosmic": FilterSpec("cosmic", "Cosmic Ray", "cosmic", "Flip bits",
                         "Flips random bits, like a stray particle through RAM. Handy for proving that an ImageStore "
                         "checksum notices.", "Cosmic ray"),
}


def compute_filter(key: str, src: np.ndarray, rect: Rect, params: dict):
    """Run a filter over *rect* of *src* (read only). Returns region values, or sparse
    ``(ys, xs, values)`` for the cosmic ray."""
    chs = params["channels"]
    if key == "blur":
        return power.gaussian_blur(src, rect, params["radius"], params["strength"], chs)
    if key == "invert":
        return power.invert(src, rect, chs)
    if key == "nuke":
        return power.nuke(src, rect, params["heat"], chs)
    if key == "cosmic":
        return power.cosmic_ray(src, rect, params["count"], params["seed"], chs)
    raise KeyError(key)


def filter_label(key: str, params: dict) -> str:
    names = "".join("RGBA"[c] for c in params["channels"])
    if key == "blur":
        extra = f" {params['strength']:g}%" if params["strength"] < 100 else ""
        return f"Gaussian blur {params['radius']:g} px{extra} ({names})"
    if key == "invert":
        return f"Invert {names}"
    if key == "nuke":
        return f"Nuke: {power.nuke_level(params['heat']).lower()} ({params['heat']:g}%)"
    return f"Cosmic ray: {params['count']:,} bit{'s' if params['count'] != 1 else ''} flipped"


# ------------------------------------------------------------------------- widgets


class HazardStripe(QWidget):
    """Diagonal hazard tape in the warning colour: the fence around the power tools."""

    def __init__(self, *, width: int | None = None, height: int | None = None, radius: float = 0, parent=None):
        super().__init__(parent)
        self.radius = radius
        if width is not None:
            self.setFixedWidth(width)
        if height is not None:
            self.setFixedHeight(height)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event) -> None:
        t = style.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, self.radius, self.radius)
        p.setClipPath(clip)
        p.fillRect(r, QColor(t["warning_subtle"]))
        stripe = QColor(t["warning"])
        stripe.setAlpha(170)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(stripe)
        band = 5.0
        x = -r.height()
        while x < r.width():
            path = QPainterPath(QPointF(x, r.height()))
            path.lineTo(x + band, r.height())
            path.lineTo(x + band + r.height(), 0)
            path.lineTo(x + r.height(), 0)
            path.closeSubpath()
            p.drawPath(path)
            x += band * 2
        p.end()


class PowerToolsSection(QFrame):
    """The toolbar's Power Tools section: hazard tape, then one button per tool."""

    def __init__(self, actions, shape_menu=None, parent=None):
        super().__init__(parent)
        self.setProperty("role", "power")
        self.setAccessibleName("Power Tools")
        self.setToolTip("Power Tools: they compute values instead of preserving them. Everything is previewed first "
                        "and one undo away.")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 3, 0)
        lay.setSpacing(1)
        lay.addWidget(HazardStripe(width=7, radius=6))
        lay.addSpacing(2)
        self.buttons: list[QToolButton] = []
        for act in actions:
            if act is None:
                line = QFrame()
                line.setFixedSize(1, 18)
                line.setStyleSheet(f"background: {style.tokens()['border']};")
                lay.addWidget(line)
                continue
            b = QToolButton()
            b.setDefaultAction(act)
            b.setIconSize(QSize(18, 18))
            b.setAutoRaise(True)
            if shape_menu is not None and act.objectName() == "tool-shape":
                b.setMenu(shape_menu)
                b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            lay.addWidget(b)
            self.buttons.append(b)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class Magnifier(QWidget):
    """Before and after of a small crop at 300%, nearest-neighbour: every pixel visible."""

    def __init__(self, parent=None):
        super().__init__(parent)
        side = MAGNIFIER_SPAN * MAGNIFIER_SCALE
        self.setFixedSize(side * 2 + 12, side + 24)
        self.images: list[QImage | None] = [None, None]
        self._keep = []
        self.caption = ""
        self.where = ""

    def set_crops(self, before: np.ndarray, after: np.ndarray, mode: str, x0: int, y0: int, *,
                  tint_changes: bool, caption: str) -> None:
        arrays = [display.to_display_rgb(before, mode, "composite", x0, y0, 1, MAGNIFIER_SCALE),
                  display.to_display_rgb(after, mode, "composite", x0, y0, 1, MAGNIFIER_SCALE)]
        if tint_changes:
            display.tint_changed(arrays[1], (before != after).any(axis=2))
        self._keep = arrays
        self.images = [QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format.Format_RGB888)
                       for a in arrays]
        self.caption = caption
        self.where = f"({x0}, {y0}) – ({x0 + before.shape[1] - 1}, {y0 + before.shape[0] - 1})"
        self.update()

    def paintEvent(self, event) -> None:
        t = style.tokens()
        p = QPainter(self)
        side = MAGNIFIER_SPAN * MAGNIFIER_SCALE
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        for i, (img, label) in enumerate(zip(self.images, ("Before", self.caption or "After"))):
            box = QRectF(i * (side + 12), 0, side, side)
            p.fillRect(box, QColor(t["canvas"]))
            if img is not None:
                target = QRectF(box.left(), box.top(), img.width() * MAGNIFIER_SCALE, img.height() * MAGNIFIER_SCALE)
                p.drawImage(target, img)
            pen = QPen(QColor(t["warning"] if i else t["border_strong"]))
            pen.setCosmetic(True)
            p.setPen(pen)
            p.drawRect(box.adjusted(0, 0, -1, -1))
            p.setFont(style.ui_font(0.86, style.QFont.Weight.DemiBold))
            p.setPen(QColor(t["text_muted"] if not i else t["warning"]))
            p.drawText(QRectF(box.left(), side + 4, side, 18), Qt.AlignmentFlag.AlignLeft, label)
        p.setFont(style.mono_font(0.82))
        p.setPen(QColor(t["text_faint"]))
        p.drawText(QRectF(0, side + 4, side, 18), Qt.AlignmentFlag.AlignRight, self.where)
        p.end()


class FineSlider(QWidget):
    """A slider for coarse moves plus a decimal spin box for exact values. The slider can
    map non-linearly so small values get most of its travel."""

    def __init__(self, lo: float, hi: float, value: float, decimals: int, suffix: str, name: str, *,
                 curve: float = 1.0, parent=None):
        super().__init__(parent)
        self.lo, self.hi, self.curve = lo, hi, curve
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setAccessibleName(name)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setDecimals(decimals)
        self.spin.setSingleStep(10 ** -decimals if decimals else 1)
        self.spin.setSuffix(suffix)
        self.spin.setFont(style.mono_font())
        self.spin.setAccessibleName(name)
        self.spin.setKeyboardTracking(False)
        self.spin.setFixedWidth(108)
        self.spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.valueChanged = self.spin.valueChanged
        self.spin.setValue(value)
        self._from_spin(value)

    def value(self) -> float:
        return self.spin.value()

    def setValue(self, v: float) -> None:  # noqa: N802
        self.spin.setValue(v)

    def _from_slider(self, pos: int) -> None:
        if self.slider.signalsBlocked():
            return
        v = self.lo + (self.hi - self.lo) * (pos / 1000) ** self.curve
        self.spin.setValue(v)

    def _from_spin(self, v: float) -> None:
        frac = 0.0 if self.hi == self.lo else (v - self.lo) / (self.hi - self.lo)
        self.slider.blockSignals(True)
        self.slider.setValue(round(1000 * max(frac, 0.0) ** (1 / self.curve)))
        self.slider.blockSignals(False)


# -------------------------------------------------------------------------- dialog


class PowerFilterDialog(QDialog):
    """One filter, fenced: parameters, a live magnifier, and Preview / Apply / Undo.

    Modeless, so the canvas stays usable for panning and zooming around the preview.
    """

    def __init__(self, window, key: str):
        super().__init__(window)
        self.window_ = window
        self.spec = FILTERS[key]
        self.key = key
        doc = window.doc
        self.doc = doc
        self.target = window.power_target()
        self.setWindowTitle(f"{self.spec.title} — Power Tools")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(HazardStripe(height=6))
        body = QVBoxLayout()
        body.setContentsMargins(18, 14, 18, 16)
        body.setSpacing(10)
        outer.addLayout(body)

        head = QHBoxLayout()
        head.setSpacing(12)
        badge = QLabel()
        badge.setFixedSize(40, 40)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(f"background: {style.tokens()['warning_subtle']}; border-radius: 9px;")
        badge.setPixmap(style.icon(self.spec.icon, "warning").pixmap(QSize(24, 24)))
        head.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(self.spec.title)
        style.set_role(title, "title")
        titles.addWidget(title)
        tagline = muted(self.spec.tagline)
        titles.addWidget(tagline)
        head.addLayout(titles, 1)
        body.addLayout(head)

        where = QHBoxLayout()
        where.addWidget(section("Target"))
        self.target_label = QLabel()
        where.addWidget(self.target_label, 1)
        body.addLayout(where)

        self.magnifier = Magnifier()
        body.addWidget(self.magnifier, 0, Qt.AlignmentFlag.AlignHCenter)

        body.addWidget(section("Settings"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        self.controls: dict[str, QWidget] = {}
        row = 0

        def add(label: str, widget: QWidget, tip: str = "") -> None:
            nonlocal row
            key_label = muted(label, "key", False)
            key_label.setToolTip(tip)
            widget.setToolTip(tip)
            grid.addWidget(key_label, row, 0)
            grid.addWidget(widget, row, 1, Qt.AlignmentFlag.AlignLeft if isinstance(widget, QSpinBox)
                           else Qt.AlignmentFlag(0))
            row += 1

        settings = window.settings
        if key == "blur":
            self.controls["radius"] = FineSlider(0.1, power.MAX_BLUR_RADIUS,
                                                 float(settings.value("power/blur_radius", 2.0)), 2, " px",
                                                 "Blur radius", curve=2.5)
            add("Radius", self.controls["radius"], "Standard deviation of the Gaussian, in pixels (0.01 px steps). "
                "Below 2 px an exact kernel is used, above it three box passes.")
            self.controls["strength"] = FineSlider(0.0, 100.0, float(settings.value("power/blur_strength", 100.0)),
                                                   1, " %", "Blur strength")
            add("Strength", self.controls["strength"], "Mix of blurred and original values, in 0.1 % steps.")
        elif key == "nuke":
            self.controls["heat"] = FineSlider(0.0, 100.0, float(settings.value("power/nuke_heat", 60.0)), 1, " %",
                                               "Heat")
            add("Heat", self.controls["heat"], "How cooked. Everything scales with it.")
            self.level = QLabel()
            self.level.setStyleSheet(f"color: {style.tokens()['warning']}; font-weight: 600;")
            add("Doneness", self.level)
        elif key == "cosmic":
            spin = QSpinBox()
            spin.setRange(1, 10_000_000)
            spin.setValue(int(settings.value("power/cosmic_count", 64)))
            spin.setFont(style.mono_font())
            spin.setSuffix(" bits")
            spin.setGroupSeparatorShown(True)
            spin.setKeyboardTracking(False)
            spin.setFixedWidth(160)
            self.controls["count"] = spin
            add("Flip", spin, "Distinct bits to flip. Each flip changes one bit of one channel byte.")
            seed = QSpinBox()
            seed.setRange(0, 2**31 - 1)
            seed.setValue(int(settings.value("power/cosmic_seed", 921)))
            seed.setFont(style.mono_font())
            seed.setKeyboardTracking(False)
            seed.setFixedWidth(160)
            self.controls["seed"] = seed
            add("Seed", seed, "Same seed, same bits: the damage is reproducible.")

        channels = QHBoxLayout()
        channels.setSpacing(10)
        self.channel_boxes: list[QCheckBox] = []
        default = list(window.tool_settings.mask)
        if key == "invert" or key == "nuke":
            default[3] = False
        for i, name in enumerate(doc.channel_names):
            box = QCheckBox()
            box.setChecked(default[i] and not (key == "nuke" and i == 3))
            box.setEnabled(not (key == "nuke" and i == 3))
            box.setAccessibleName(f"Change {name}")
            pair = QHBoxLayout()
            pair.setSpacing(2)
            pair.addWidget(box)
            pair.addWidget(chip(name))
            channels.addLayout(pair)
            self.channel_boxes.append(box)
            box.toggled.connect(self._changed)
        channels.addStretch(1)
        holder = QWidget()
        holder.setLayout(channels)
        add("Channels", holder, "Only ticked channels are computed and written; the rest keep their exact bytes."
            + (" The nuker never touches alpha." if key == "nuke" else ""))
        body.addLayout(grid)
        self.note = muted("", "faint")
        body.addWidget(self.note)

        banner = QFrame()
        banner.setProperty("banner", "warn")
        bl = QHBoxLayout(banner)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(8)
        glyph = QLabel()
        glyph.setPixmap(style.icon("alert", "warning").pixmap(QSize(18, 18)))
        bl.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        self.banner_text = muted("")
        bl.addWidget(self.banner_text, 1)
        body.addWidget(banner)

        footer = QHBoxLayout()
        self.actions = PowerActions(self.spec.verb)
        row = self.actions.row
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        row.insertWidget(row.indexOf(self.actions.preview_button), self.close_button)
        footer.addWidget(self.actions)
        body.addLayout(footer)

        self.actions.previewToggled.connect(self._preview_toggled)
        self.actions.applyRequested.connect(self.apply)
        self.actions.undoRequested.connect(window.undo_power)
        for name, w in self.controls.items():
            w.valueChanged.connect(self._changed)
        canvas = window.canvas
        for signal in (canvas.zoomChanged, canvas.horizontalScrollBar().valueChanged,
                       canvas.verticalScrollBar().valueChanged):
            signal.connect(self._view_moved)
        self._magnify_timer = QTimer(self, singleShot=True, interval=60)
        self._magnify_timer.timeout.connect(self._update_magnifier)
        self._preview_timer = QTimer(self, singleShot=True, interval=180)
        self._preview_timer.timeout.connect(self._live_preview)
        self.focus = (self.target.x + self.target.w // 2, self.target.y + self.target.h // 2)
        self.setFixedWidth(self.magnifier.width() + 2 * 18 + 40)
        self._describe_target()
        self._changed()
        self._update_magnifier()

    def _fit(self) -> None:
        """Grow to fit wrapped text: Qt's minimum size ignores height-for-width, which would
        let the fixed-size magnifier overlap the settings."""
        needed = self.layout().totalHeightForWidth(self.width())
        if needed > 0:
            self.setMinimumHeight(needed)
            if self.height() < needed:
                self.resize(self.width(), needed)

    def focus_on(self, x: int, y: int) -> None:
        """Move the magnifier to an inspected pixel inside the target."""
        r = self.target
        if r.x <= x < r.x1 and r.y <= y < r.y1:
            self.focus = (x, y)
            self._magnify_timer.start()

    # ---------------------------------------------------------------- parameters

    def params(self) -> dict:
        p = {"channels": [i for i, b in enumerate(self.channel_boxes) if b.isChecked()]}
        for name, w in self.controls.items():
            p[name] = w.value()
        return p

    def settings_key(self) -> tuple:
        p = self.params()
        return (self.key, self.target, tuple(p["channels"]),
                *(round(v, 4) if isinstance(v, float) else v for k, v in sorted(p.items()) if k != "channels"))

    @property
    def heavy(self) -> bool:
        return self.target.w * self.target.h > LIVE_PIXELS

    def preview_rect(self) -> Rect:
        """What a preview computes: the whole target, or for a large target just the part on
        screen (when that part is small enough to recompute live)."""
        if not self.heavy or self.key == "cosmic":
            return self.target
        vis = self.window_.canvas.visible_image_rect()
        t = self.target
        if vis is None:
            return t
        x0, y0, x1, y1 = max(vis.x, t.x), max(vis.y, t.y), min(vis.x1, t.x1), min(vis.y1, t.y1)
        if x0 >= x1 or y0 >= y1 or (x1 - x0) * (y1 - y0) > LIVE_PIXELS:
            return t
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def _mine(self) -> Pending | None:
        pending = self.window_.pending
        return pending if pending is not None and pending.source == self.key else None

    def _view_moved(self, *_) -> None:
        pending = self._mine()
        if pending is not None and pending.partial:
            self._preview_timer.start()
    def _describe_target(self) -> None:
        r, doc = self.target, self.doc
        whole = (r.w, r.h) == (doc.width, doc.height)
        text = f"Whole image, {r.w} × {r.h}" if whole else f"Selection, {r.w} × {r.h} at ({r.x}, {r.y})"
        self.target_label.setText(f"{text} ({r.w * r.h:,} pixels)")

    def _changed(self, *_) -> None:
        p = self.params()
        persist = {"radius": "blur_radius", "strength": "blur_strength", "heat": "nuke_heat",
                   "count": "cosmic_count", "seed": "cosmic_seed"}
        for name, key in persist.items():
            if name in p:
                self.window_.settings.setValue(f"power/{key}", p[name])
        n = self.target.w * self.target.h
        if self.key == "blur":
            _, divisor, reach = power.blur_plan(p["radius"])
            method = "An exact sampled kernel" if p["radius"] < 2 else "Three box passes"
            self.note.setText(f"{method} approximate σ = {p['radius']:g} px, reading up to {reach} px beyond the "
                              "target and extending the image edge. Integer arithmetic: the same settings give "
                              "the same bytes on every machine.")
        elif self.key == "nuke":
            self.level.setText(power.nuke_level(p["heat"]))
            self.note.setText("Grain and blocks follow absolute coordinates, so the preview is exactly what you get.")
        elif self.key == "invert":
            self.note.setText("Apply twice and every byte is back. The integrity chips will agree.")
        else:
            total = n * len(p["channels"]) * 8
            self.note.setText(f"{min(p['count'], total):,} of {total:,} candidate bits "
                              f"({100 * min(p['count'], total) / max(total, 1):.4g}%). Changed pixels are tinted "
                              "in the magnifier.")
        if not p["channels"]:
            self.banner_text.setText("No channels ticked: nothing would change.")
        else:
            verb = "flips bits in" if self.key == "cosmic" else "computes new values for"
            self.banner_text.setText(
                f"{self.spec.title} {verb} up to {n:,} pixels. Nothing is written until you press "
                f"{self.spec.verb}; the result is one undoable step, and the integrity chips will count every byte.")
        self._fit()
        self._magnify_timer.start()
        pending = self._mine()
        if pending is not None:
            if pending.partial or not self.heavy:
                self._preview_timer.start()
            else:
                self.window_.clear_pending()
                self.note.setText(self.note.text() + " Settings changed, so press Preview again.")
        self.refresh_state()

    # ------------------------------------------------------------------ preview

    def _magnifier_crop(self) -> Rect:
        r = self.target
        cx, cy = self.focus
        span = MAGNIFIER_SPAN
        x0 = min(max(cx - span // 2, r.x), max(r.x, r.x1 - span))
        y0 = min(max(cy - span // 2, r.y), max(r.y, r.y1 - span))
        return Rect(x0, y0, min(span, r.w), min(span, r.h))

    def _update_magnifier(self) -> None:
        doc = self.doc
        if doc is None or self.window_.doc is not doc:
            return
        crop = self._magnifier_crop()
        p = self.params()
        before = doc.pixels[crop.y:crop.y1, crop.x:crop.x1]
        if self.key == "cosmic":
            ys, xs, vals = self._cosmic_points(p)
            after = before.copy()
            sel = (ys >= crop.y) & (ys < crop.y1) & (xs >= crop.x) & (xs < crop.x1)
            after[ys[sel] - crop.y, xs[sel] - crop.x] = vals[sel]
            hits = int(sel.sum())
            caption = f"After ({hits} pixel{'s' if hits != 1 else ''} hit)"
        else:
            after = compute_filter(self.key, doc.pixels, crop, p) if p["channels"] else before
            caption = "After"
        self.magnifier.set_crops(before, after, doc.mode, crop.x, crop.y, tint_changes=self.key == "cosmic",
                                 caption=caption)

    def _cosmic_points(self, p: dict):
        key = (self.doc.generation, self.target, p["count"], p["seed"], tuple(p["channels"]))
        if getattr(self, "_cosmic_cache", (None,))[0] != key:
            self._cosmic_cache = (key, power.cosmic_ray(self.doc.pixels, self.target, p["count"], p["seed"],
                                                        p["channels"]))
        return self._cosmic_cache[1]

    def compute(self, rect: Rect | None = None) -> Pending | None:
        """Compute the result for *rect* (default: the whole target), off the GUI thread
        when it is big."""
        p = self.params()
        if not p["channels"]:
            return None
        doc, rect = self.doc, rect or self.target
        if self.key == "cosmic":
            ys, xs, vals = self._cosmic_points(p)
            if len(ys) == 0:
                return None
            bbox = Rect.from_points(int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
            return Pending(filter_label(self.key, p), self.key, bbox, doc.generation, points=(ys, xs, vals),
                           settings=self.settings_key(), verb=self.spec.verb)
        values = self.window_.run_heavy(f"Computing {self.spec.title.lower()}…", rect.w * rect.h,
                                        compute_filter, self.key, doc.pixels, rect, p)
        return Pending(filter_label(self.key, p), self.key, rect, doc.generation, values=values,
                       settings=self.settings_key(), verb=self.spec.verb, partial=rect != self.target)

    def _fresh(self, pending: Pending | None, rect: Rect) -> bool:
        return pending is not None and pending.rect == rect and pending.is_current(self.doc, self.settings_key())

    def _preview_toggled(self, on: bool) -> None:
        if not on:
            if self._mine() is not None:
                self.window_.clear_pending()
            return
        rect = self.preview_rect()
        if self._fresh(self._mine(), rect) or (self.key == "cosmic" and self._mine() is not None
                                               and self._mine().is_current(self.doc, self.settings_key())):
            self.window_.show_pending(True)
            return
        self.window_.set_pending(self.compute(rect))

    def _live_preview(self) -> None:
        pending = self._mine()
        if pending is None:
            return
        rect = self.preview_rect() if pending.partial or not self.heavy else self.target
        if self.key == "cosmic":
            if not pending.is_current(self.doc, self.settings_key()):
                self.window_.set_pending(self.compute())
        elif not self._fresh(pending, rect):
            self.window_.set_pending(self.compute(rect))

    def apply(self) -> None:
        pending = self._mine()
        if self.key == "cosmic":
            fresh = pending is not None and pending.is_current(self.doc, self.settings_key())
        else:
            fresh = self._fresh(pending, self.target)
        if not fresh:  # partial or stale previews are recomputed for the whole target
            pending = self.compute()
        if pending is None:
            self.window_.statusBar().showMessage(f"{self.spec.title}: nothing to change", 5000)
            return
        self.window_.apply_pending(pending)

    def refresh_state(self) -> None:
        pending = self.window_.pending
        mine = pending is not None and pending.source == self.key and self.window_.canvas.show_pending
        self.actions.set_state(can_apply=bool(self.params()["channels"]), can_undo=self.window_.can_undo_power(),
                               previewing=mine)

    def document_changed(self) -> None:
        doc = self.window_.doc
        if doc is not self.doc or doc is None or self.target.clamp(doc.width, doc.height) != self.target:
            self.close()
            return
        self._magnify_timer.start()
        pending = self.window_.pending
        if pending is not None and pending.source == self.key and pending.generation != doc.generation:
            self.window_.clear_pending()
        self.refresh_state()

    def closeEvent(self, event) -> None:
        if self.window_.pending is not None and self.window_.pending.source == self.key:
            self.window_.clear_pending()
        if self.window_.power_dialog is self:
            self.window_.power_dialog = None
        super().closeEvent(event)
