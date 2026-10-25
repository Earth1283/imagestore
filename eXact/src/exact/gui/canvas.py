"""Zoomable, viewport-rendered pixel views.

Only the visible region is ever converted for display: on each paint the view slices the
visible rectangle out of the source, converts that slice to an opaque RGB array and
draws it scaled with nearest-neighbour sampling. No full-image display copy exists.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QImage, QPainter, QPen
from PyQt6.QtWidgets import QAbstractScrollArea

from ..core.document import Clip, Document, Rect
from . import display

ZOOM_LEVELS = [0.125, 0.25, 0.5, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32]
GRID_MIN_ZOOM = 4
TILE = 256  # display samples per tile side
TILE_CACHE_BYTES = 96 * 1024 * 1024


def zoom_label(zoom: float) -> str:
    return f"{zoom * 100:g}%"


class DocumentSource:
    """Renders the editor's document, with before/after and change highlighting."""

    def __init__(self, doc: Document):
        self.doc = doc
        self.display_mode = "composite"
        self.compare = "current"  # "current" | "original" | "split"
        self.split = 0.5  # fraction of the width shown as original in split mode
        self.highlight_changes = False

    @property
    def width(self) -> int:
        return self.doc.width

    @property
    def height(self) -> int:
        return self.doc.height

    @property
    def split_x(self) -> int:
        return int(round(self.width * self.split))

    @property
    def generation(self) -> int:
        return self.doc.generation

    def signature(self, zoom: float) -> tuple:
        cell = display.checker_cell(zoom) if self.display_mode == "composite" and self.doc.mode == "RGBA" else 0
        split = self.split_x if self.compare == "split" else -1
        return (id(self.doc), self.doc.pixels.shape, self.display_mode, self.compare, split,
                self.highlight_changes, cell)

    def render(self, x0: int, y0: int, x1: int, y1: int, step: int, zoom: float) -> np.ndarray:
        doc = self.doc
        cur = doc.pixels[y0:y1:step, x0:x1:step]
        need_ref = self.compare != "current" or self.highlight_changes
        ref = doc.reference()[y0:y1:step, x0:x1:step] if need_ref else None
        if ref is not None and ref.shape != cur.shape:
            # A worker thread is between swapping the buffer and updating the orientation.
            return display.to_display_rgb(cur, doc.mode, self.display_mode, x0, y0, step, zoom)
        if self.compare == "original":
            base = ref
        elif self.compare == "split":
            xs = np.arange(x0, x1, step)[: cur.shape[1]]
            base = np.where((xs < self.split_x)[None, :, None], ref, cur)
        else:
            base = cur
        rgb = display.to_display_rgb(base, doc.mode, self.display_mode, x0, y0, step, zoom)
        if self.highlight_changes:
            display.tint_changed(rgb, (cur != ref).any(axis=2))
        return rgb


class HeatmapSource:
    def __init__(self, heat: np.ndarray):
        self.heat = heat

    @property
    def width(self) -> int:
        return self.heat.shape[1]

    @property
    def height(self) -> int:
        return self.heat.shape[0]

    generation = 0

    def signature(self, zoom: float) -> tuple:
        return (id(self.heat),)

    def render(self, x0, y0, x1, y1, step, zoom) -> np.ndarray:
        return display.heat_colors(display.max_pool(self.heat[y0:y1, x0:x1], step))


class TileCache:
    """LRU of rendered display tiles. Keys are (signature, step, tx, ty); a tile covers
    image pixels [tx*TILE*step, (tx+1)*TILE*step) horizontally, likewise vertically.

    Invalidation can arrive from a worker thread (full-image transforms), hence the lock.
    """

    def __init__(self, max_bytes: int = TILE_CACHE_BYTES):
        self.max_bytes = max_bytes
        self._tiles: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._tiles.get(key)
            if entry is not None:
                self._tiles.move_to_end(key)
                return entry[1]
            return None

    def put(self, key, array: np.ndarray, image: QImage) -> None:
        with self._lock:
            if key in self._tiles:
                return
            self._tiles[key] = (array, image)
            self._bytes += array.nbytes
            while self._bytes > self.max_bytes and self._tiles:
                _, (old, _) = self._tiles.popitem(last=False)
                self._bytes -= old.nbytes

    def invalidate(self, rect: Rect | None) -> None:
        with self._lock:
            if rect is None:
                self._tiles.clear()
                self._bytes = 0
                return
            for key in list(self._tiles):
                _, step, tx, ty = key
                span = TILE * step
                if (tx * span < rect.x1 and rect.x < (tx + 1) * span
                        and ty * span < rect.y1 and rect.y < (ty + 1) * span):
                    array, _ = self._tiles.pop(key)
                    self._bytes -= array.nbytes

    def __len__(self) -> int:
        return len(self._tiles)


class PixelView(QAbstractScrollArea):
    zoomChanged = pyqtSignal(float)
    hoverChanged = pyqtSignal(object)  # (x, y) or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = None
        self.tiles = TileCache()
        self.zoom = 1.0
        self.show_grid = False
        self.overlay_rects: list[Rect] = []
        self._pan_last: QPointF | None = None
        self._space_down = False
        self._hover: tuple[int, int] | None = None
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.horizontalScrollBar().setSingleStep(24)
        self.verticalScrollBar().setSingleStep(24)
        self._background = QColor(48, 48, 52)


    def set_source(self, source, *, keep_view: bool = False) -> None:
        self.source = source
        self.tiles.invalidate(None)
        if not keep_view:
            self.horizontalScrollBar().setValue(0)
            self.verticalScrollBar().setValue(0)
        self._update_scrollbars()
        self.viewport().update()

    def image_size(self) -> tuple[int, int]:
        if self.source is None:
            return 0, 0
        return self.source.width, self.source.height

    def _offsets(self) -> tuple[float, float, int, int]:
        w, h = self.image_size()
        vw, vh = self.viewport().width(), self.viewport().height()
        ox = max(0.0, (vw - w * self.zoom) / 2)
        oy = max(0.0, (vh - h * self.zoom) / 2)
        return ox, oy, self.horizontalScrollBar().value(), self.verticalScrollBar().value()

    def view_to_image(self, pos: QPointF) -> tuple[float, float]:
        ox, oy, sx, sy = self._offsets()
        return (pos.x() - ox + sx) / self.zoom, (pos.y() - oy + sy) / self.zoom

    def image_to_view(self, x: float, y: float) -> QPointF:
        ox, oy, sx, sy = self._offsets()
        return QPointF(x * self.zoom - sx + ox, y * self.zoom - sy + oy)

    def pixel_at(self, pos: QPointF, *, clamp: bool = False) -> tuple[int, int] | None:
        if self.source is None:
            return None
        fx, fy = self.view_to_image(pos)
        x, y = math.floor(fx), math.floor(fy)
        w, h = self.image_size()
        if clamp:
            return min(max(x, 0), w - 1), min(max(y, 0), h - 1)
        if 0 <= x < w and 0 <= y < h:
            return x, y
        return None

    def raw_pixel_at(self, pos: QPointF) -> tuple[int, int]:
        fx, fy = self.view_to_image(pos)
        return math.floor(fx), math.floor(fy)

    def image_rect_to_view(self, rect: Rect) -> QRectF:
        tl = self.image_to_view(rect.x, rect.y)
        return QRectF(tl.x(), tl.y(), rect.w * self.zoom, rect.h * self.zoom)

    def update_image_rect(self, rect: Rect | None) -> None:
        if rect is None:
            return
        self.viewport().update(self.image_rect_to_view(rect).toAlignedRect().adjusted(-2, -2, 2, 2))

    def _update_scrollbars(self) -> None:
        w, h = self.image_size()
        vw, vh = self.viewport().width(), self.viewport().height()
        cw, ch = math.ceil(w * self.zoom), math.ceil(h * self.zoom)
        hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
        hb.setRange(0, max(0, cw - vw))
        vb.setRange(0, max(0, ch - vh))
        hb.setPageStep(max(1, vw))
        vb.setPageStep(max(1, vh))


    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        zoom = min(max(zoom, ZOOM_LEVELS[0]), ZOOM_LEVELS[-1])
        if zoom == self.zoom:
            return
        if anchor is None:
            anchor = QPointF(self.viewport().width() / 2, self.viewport().height() / 2)
        fx, fy = self.view_to_image(anchor)
        self.zoom = zoom
        self._update_scrollbars()
        ox, oy, _, _ = self._offsets()
        self.horizontalScrollBar().setValue(round(fx * zoom + ox - anchor.x()))
        self.verticalScrollBar().setValue(round(fy * zoom + oy - anchor.y()))
        self.viewport().update()
        self.zoomChanged.emit(zoom)

    def zoom_in(self, anchor: QPointF | None = None) -> None:
        bigger = [z for z in ZOOM_LEVELS if z > self.zoom]
        if bigger:
            self.set_zoom(bigger[0], anchor)

    def zoom_out(self, anchor: QPointF | None = None) -> None:
        smaller = [z for z in ZOOM_LEVELS if z < self.zoom]
        if smaller:
            self.set_zoom(smaller[-1], anchor)

    def zoom_to_fit(self) -> None:
        w, h = self.image_size()
        if not w:
            return
        vw, vh = self.viewport().width(), self.viewport().height()
        fitting = [z for z in ZOOM_LEVELS if w * z <= vw and h * z <= vh]
        self.set_zoom(fitting[-1] if fitting else ZOOM_LEVELS[0])

    def center_on(self, x: float, y: float) -> None:
        ox, oy, _, _ = self._offsets()
        self.horizontalScrollBar().setValue(round(x * self.zoom + ox - self.viewport().width() / 2))
        self.verticalScrollBar().setValue(round(y * self.zoom + oy - self.viewport().height() / 2))
        self.viewport().update()

    def set_grid(self, on: bool) -> None:
        self.show_grid = on
        self.viewport().update()


    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_scrollbars()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in(event.position())
            elif delta < 0:
                self.zoom_out(event.position())
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = True
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = False
            self._restore_cursor()
            return
        super().keyReleaseEvent(event)

    def _restore_cursor(self) -> None:
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def _wants_pan(self, event) -> bool:
        return event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and self._space_down)

    def mousePressEvent(self, event) -> None:
        if self._wants_pan(event):
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y()))
            return
        self._set_hover(self.pixel_at(event.position()))

    def mouseReleaseEvent(self, event) -> None:
        if self._pan_last is not None:
            self._pan_last = None
            if self._space_down:
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self._restore_cursor()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover(None)
        super().leaveEvent(event)

    def _set_hover(self, pixel) -> None:
        if pixel != self._hover:
            old = self._hover
            self._hover = pixel
            if self.zoom >= GRID_MIN_ZOOM:
                for p in (old, pixel):
                    if p:
                        self.update_image_rect(Rect(p[0], p[1], 1, 1))
            self.hoverChanged.emit(pixel)


    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        clip = event.rect()
        painter.fillRect(clip, self._background)
        if self.source is None:
            painter.end()
            return
        w, h = self.image_size()
        z = self.zoom
        ox, oy, sx, sy = self._offsets()
        x0 = max(0, math.floor((clip.left() - ox + sx) / z))
        y0 = max(0, math.floor((clip.top() - oy + sy) / z))
        x1 = min(w, math.ceil((clip.right() + 1 - ox + sx) / z))
        y1 = min(h, math.ceil((clip.bottom() + 1 - oy + sy) / z))
        if x0 < x1 and y0 < y1:
            step = 1 if z >= 1 else int(round(1 / z))
            span = TILE * step
            content = QRectF(ox - sx, oy - sy, w * z, h * z)
            painter.save()
            painter.setClipRect(content.intersected(QRectF(clip)))
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            signature = self.source.signature(z)
            for ty in range(y0 // span, (y1 - 1) // span + 1):
                for tx in range(x0 // span, (x1 - 1) // span + 1):
                    image = self._tile(signature, step, tx, ty, z)
                    target = QRectF(tx * span * z - sx + ox, ty * span * z - sy + oy,
                                    image.width() * step * z, image.height() * step * z)
                    painter.drawImage(target, image, QRectF(0, 0, image.width(), image.height()))
            painter.restore()
            if self.show_grid and z >= GRID_MIN_ZOOM:
                self._paint_grid(painter, x0, y0, x1, y1)
        self.paint_overlays(painter)
        painter.end()

    def _tile(self, signature: tuple, step: int, tx: int, ty: int, zoom: float) -> QImage:
        key = (signature, step, tx, ty)
        image = self.tiles.get(key)
        if image is not None:
            return image
        w, h = self.image_size()
        span = TILE * step
        generation = self.source.generation
        rgb = self.source.render(tx * span, ty * span, min(w, (tx + 1) * span), min(h, (ty + 1) * span), step, zoom)
        image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888)
        if self.source.generation == generation:
            self.tiles.put(key, rgb, image)
        else:
            image = image.copy()  # rendered during a concurrent change: use once, don't cache
        return image

    def _paint_grid(self, painter: QPainter, x0: int, y0: int, x1: int, y1: int) -> None:
        pen = QPen(QColor(128, 128, 128, 140))
        pen.setCosmetic(True)
        pen.setWidth(0)
        painter.setPen(pen)
        top, bottom = self.image_to_view(x0, y0).y(), self.image_to_view(x0, y1).y()
        left, right = self.image_to_view(x0, y0).x(), self.image_to_view(x1, y0).x()
        lines = []
        for x in range(x0, x1 + 1):
            vx = self.image_to_view(x, 0).x()
            lines.append(QLineF(vx, top, vx, bottom))
        for y in range(y0, y1 + 1):
            vy = self.image_to_view(0, y).y()
            lines.append(QLineF(left, vy, right, vy))
        painter.drawLines(lines)

    def _outline(self, painter: QPainter, rect: Rect, color: QColor, dashed: bool = False) -> None:
        r = self.image_rect_to_view(rect)
        pen = QPen(QColor(0, 0, 0) if dashed else color)
        pen.setCosmetic(True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        painter.drawRect(r.adjusted(0, 0, -1, -1) if r.width() > 2 else r)
        if dashed:
            pen2 = QPen(color)
            pen2.setCosmetic(True)
            pen2.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen2)
            painter.drawRect(r.adjusted(0, 0, -1, -1) if r.width() > 2 else r)

    def paint_overlays(self, painter: QPainter) -> None:
        for rect in self.overlay_rects:
            self._outline(painter, rect, QColor(0, 220, 255), dashed=True)


class EditorCanvas(PixelView):
    strokeFinished = pyqtSignal(object)  # OperationSummary
    pixelPicked = pyqtSignal(int, int)
    colorPicked = pyqtSignal(object)  # tuple of channel values
    selectionChanged = pyqtSignal(object)  # Rect | None
    pasteCommitted = pyqtSignal(int, int)
    pasteCancelled = pyqtSignal()

    TOOLS = ("inspect", "pencil", "select", "hand")

    def __init__(self, tool_settings, parent=None):
        super().__init__(parent)
        self.tool_settings = tool_settings
        self.doc: Document | None = None
        self.doc_source: DocumentSource | None = None
        self.tool = "inspect"
        self.selection: Rect | None = None
        self.inspected: tuple[int, int] | None = None
        self.paste_clip: Clip | None = None
        self._paste_preview: QImage | None = None
        self._paste_preview_data = None
        self._select_anchor: tuple[int, int] | None = None
        self._stroking = False
        self.busy = False

    def set_document(self, doc: Document | None) -> None:
        if self.doc is not None:
            self.doc.remove_change_listener(self.tiles.invalidate)
        if doc is not None:
            doc.add_change_listener(self.tiles.invalidate)
        self.doc = doc
        self.selection = None
        self.inspected = None
        self.cancel_paste()
        old = self.doc_source
        self.doc_source = DocumentSource(doc) if doc else None
        if old and self.doc_source:
            self.doc_source.display_mode = old.display_mode
        self.set_source(self.doc_source)

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self._restore_cursor()

    def _restore_cursor(self) -> None:
        shapes = {
            "inspect": Qt.CursorShape.CrossCursor,
            "pencil": Qt.CursorShape.CrossCursor,
            "select": Qt.CursorShape.CrossCursor,
            "hand": Qt.CursorShape.OpenHandCursor,
        }
        self.viewport().setCursor(QCursor(shapes.get(self.tool, Qt.CursorShape.ArrowCursor)))

    def set_selection(self, rect: Rect | None) -> None:
        self.selection = rect
        self.viewport().update()
        self.selectionChanged.emit(rect)

    def set_inspected(self, pixel: tuple[int, int] | None) -> None:
        self.inspected = pixel
        self.viewport().update()


    def begin_paste(self, clip: Clip) -> None:
        self.paste_clip = clip
        self._paste_preview = None
        if clip.width * clip.height <= 4_000_000:
            rgb = display.to_display_rgb(clip.pixels, clip.mode, "composite", 0, 0, 1, max(self.zoom, 1))
            self._paste_preview_data = rgb
            self._paste_preview = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                                         QImage.Format.Format_RGB888)
        self.viewport().update()

    def cancel_paste(self) -> None:
        if self.paste_clip is not None:
            self.paste_clip = None
            self._paste_preview = None
            self._paste_preview_data = None
            self.viewport().update()
            self.pasteCancelled.emit()

    def finish_paste(self) -> None:
        self.paste_clip = None
        self._paste_preview = None
        self._paste_preview_data = None
        self.viewport().update()


    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self.paste_clip is not None:
            self.cancel_paste()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if self.busy or self.doc is None:
            return
        if self._wants_pan(event) or (self.tool == "hand" and event.button() == Qt.MouseButton.LeftButton):
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self.paste_clip is not None:
            if event.button() == Qt.MouseButton.LeftButton:
                x, y = self.raw_pixel_at(event.position())
                self.pasteCommitted.emit(x, y)
            elif event.button() == Qt.MouseButton.RightButton:
                self.cancel_paste()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pixel = self.pixel_at(event.position())
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        if self.tool == "pencil" and alt:
            if pixel:
                self.colorPicked.emit(self.doc.get_pixel(*pixel))
            return
        if self.tool == "pencil":
            ts = self.tool_settings
            self.doc.begin_stroke(ts.color, ts.size, ts.mask)
            self._stroking = True
            self.update_image_rect(self.doc.stroke_to(*self.raw_pixel_at(event.position())))
        elif self.tool == "select":
            self._select_anchor = self.pixel_at(event.position(), clamp=True)
            self.set_selection(Rect(*self._select_anchor, 1, 1))
        elif self.tool == "inspect" and pixel:
            self.pixelPicked.emit(*pixel)

    def mouseMoveEvent(self, event) -> None:
        if self._pan_last is not None:
            super().mouseMoveEvent(event)
            return
        self._set_hover(self.pixel_at(event.position()))
        if self.busy or self.doc is None:
            return
        if self.paste_clip is not None:
            self.viewport().update()
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._stroking:
            self.update_image_rect(self.doc.stroke_to(*self.raw_pixel_at(event.position())))
        elif self._select_anchor is not None:
            x, y = self.pixel_at(event.position(), clamp=True)
            self.selection = Rect.from_points(*self._select_anchor, x, y)
            self.viewport().update()
        elif self.tool == "inspect":
            pixel = self.pixel_at(event.position())
            if pixel:
                self.pixelPicked.emit(*pixel)

    def mouseReleaseEvent(self, event) -> None:
        if self._pan_last is not None:
            super().mouseReleaseEvent(event)
            return
        if self._stroking and event.button() == Qt.MouseButton.LeftButton:
            self._stroking = False
            summary = self.doc.end_stroke()
            self.strokeFinished.emit(summary)
        elif self._select_anchor is not None and event.button() == Qt.MouseButton.LeftButton:
            self._select_anchor = None
            self.selectionChanged.emit(self.selection)


    def paint_overlays(self, painter: QPainter) -> None:
        if self.doc is None:
            return
        src = self.doc_source
        if src and src.compare == "split":
            top = self.image_to_view(src.split_x, 0)
            pen = QPen(QColor(255, 200, 0))
            pen.setWidth(2)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(top, self.image_to_view(src.split_x, self.doc.height))
        if self.selection is not None:
            self._outline(painter, self.selection, QColor(255, 255, 255), dashed=True)
        if self.inspected is not None:
            x, y = self.inspected
            r = self.image_rect_to_view(Rect(x, y, 1, 1))
            if r.width() < 6:
                r = r.adjusted(-3, -3, 3, 3)
            pen = QPen(QColor(0, 255, 120))
            pen.setCosmetic(True)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(r)
        if self._hover and self.zoom >= GRID_MIN_ZOOM and self.paste_clip is None:
            self._outline(painter, Rect(*self._hover, 1, 1), QColor(255, 255, 0))
        if self.paste_clip is not None:
            pos = self.viewport().mapFromGlobal(QCursor.pos())
            x, y = self.raw_pixel_at(QPointF(pos))
            target = Rect(x, y, self.paste_clip.width, self.paste_clip.height)
            if self._paste_preview is not None:
                painter.save()
                painter.setOpacity(0.75)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                painter.drawImage(self.image_rect_to_view(target), self._paste_preview)
                painter.restore()
            self._outline(painter, target, QColor(255, 140, 0), dashed=True)
        super().paint_overlays(painter)

