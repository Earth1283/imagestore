"""Shared visual identity for ImageStore and eXact: colour tokens, Qt theme and icons.

This file exists twice, byte-for-byte identical:

    eXact/src/exact/gui/style.py
    imagestore/imagestore_gui/style.py

so that both applications stay independently installable while looking like one family.
A test in each project fails if the copies drift apart; edit one and copy it over.

Design rules encoded here:

* Dark by default, light available. Surfaces step up in lightness from ``base`` (canvas
  surround) to ``window`` (chrome) to ``panel`` (docks) to ``raised`` (cards, menus).
* One restrained accent. Status colours (success / warning / danger) are reserved for
  integrity and error states, never decoration.
* Channel colours (R, G, B, A) label raw channel values; they are labels, not data.
* Icons are stroke-based SVG drawn in ``currentColor`` and rendered at the device pixel
  ratio of the target, so they stay crisp on HiDPI / Retina screens.
"""

from __future__ import annotations

from functools import lru_cache

from PyQt6.QtCore import QByteArray, QPoint, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PyQt6.QtSvg import QSvgRenderer

MOTTO = "Code is Art.*"
MOTTO_FOOTNOTE = "*Handle with care."

DARK = {
    "base": "#131417",
    "window": "#1a1c20",
    "panel": "#1f2125",
    "raised": "#272a30",
    "input": "#16181b",
    "hover": "#2d3037",
    "pressed": "#353942",
    "border": "#30333a",
    "border_strong": "#434750",
    "text": "#e6e8eb",
    "text_muted": "#a2a7b0",
    "text_faint": "#80858e",
    "text_disabled": "#5b5f67",
    "icon": "#c3c7ce",
    "accent": "#5b93ff",
    "accent_hover": "#77a6ff",
    "accent_text": "#ffffff",
    "accent_subtle": "#22324d",
    "selection": "#2c4878",
    "success": "#44c283",
    "success_subtle": "#18352a",
    "warning": "#e5ac52",
    "warning_subtle": "#3a2e1b",
    "danger": "#f06a66",
    "danger_subtle": "#3f2227",
    "ch_r": "#f07575",
    "ch_g": "#52c785",
    "ch_b": "#6aa5ff",
    "ch_a": "#b8bcc4",
    "canvas": "#101113",
    "checker_light": "#cccccc",
    "checker_dark": "#999999",
}

LIGHT = {
    "base": "#e6e8ec",
    "window": "#f3f4f6",
    "panel": "#fafbfc",
    "raised": "#ffffff",
    "input": "#ffffff",
    "hover": "#e9ebef",
    "pressed": "#dde0e5",
    "border": "#d5d8dd",
    "border_strong": "#bfc3ca",
    "text": "#1c1f24",
    "text_muted": "#555b65",
    "text_faint": "#737983",
    "text_disabled": "#a5aab1",
    "icon": "#3b4048",
    "accent": "#2f6fe4",
    "accent_hover": "#2560cc",
    "accent_text": "#ffffff",
    "accent_subtle": "#dce7fb",
    "selection": "#c4d7f8",
    "success": "#1d8a53",
    "success_subtle": "#daf1e4",
    "warning": "#9a6208",
    "warning_subtle": "#fbeed6",
    "danger": "#c4383a",
    "danger_subtle": "#fbe0df",
    "ch_r": "#cf3f3f",
    "ch_g": "#25914f",
    "ch_b": "#2f6fe4",
    "ch_a": "#646a73",
    "canvas": "#d9dce1",
    "checker_light": "#cccccc",
    "checker_dark": "#999999",
}

THEMES = {"dark": DARK, "light": LIGHT}
_tokens: dict[str, str] = dict(DARK)
_theme_name = "dark"


def tokens() -> dict[str, str]:
    """The active colour tokens (a live dict: it changes when the theme changes)."""
    return _tokens


def color(name: str) -> QColor:
    return QColor(_tokens[name])


def theme_name() -> str:
    return _theme_name


# ----------------------------------------------------------------------------- fonts

MONO_FAMILIES = ("JetBrains Mono", "SF Mono", "Menlo", "Cascadia Mono", "Consolas", "Source Code Pro",
                 "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", "Courier New")


@lru_cache(maxsize=1)
def _mono_family() -> str | None:
    available = set(QFontDatabase.families())
    for family in MONO_FAMILIES:
        if family in available:
            return family
    return None


def mono_font(scale: float = 1.0) -> QFont:
    """A monospaced font at roughly the UI font size (for bytes, hashes, coordinates)."""
    family = _mono_family()
    font = QFont(family) if family else QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setStyleHint(QFont.StyleHint.Monospace)
    base = QGuiApplication.font().pointSizeF()
    if base > 0:
        font.setPointSizeF(base * scale)
    return font


def ui_font(scale: float = 1.0, weight: QFont.Weight | None = None) -> QFont:
    font = QFont(QGuiApplication.font())
    if font.pointSizeF() > 0:
        font.setPointSizeF(font.pointSizeF() * scale)
    if weight is not None:
        font.setWeight(weight)
    return font


# ---------------------------------------------------------------------------- theme


def _palette(t: dict[str, str]) -> QPalette:
    p = QPalette()
    roles = QPalette.ColorRole
    for role, key in (
        (roles.Window, "window"), (roles.WindowText, "text"), (roles.Base, "input"),
        (roles.AlternateBase, "panel"), (roles.ToolTipBase, "raised"), (roles.ToolTipText, "text"),
        (roles.PlaceholderText, "text_faint"), (roles.Text, "text"), (roles.Button, "raised"),
        (roles.ButtonText, "text"), (roles.BrightText, "danger"), (roles.Light, "border_strong"),
        (roles.Midlight, "border"), (roles.Mid, "border_strong"), (roles.Dark, "base"), (roles.Shadow, "base"),
        (roles.Highlight, "selection"), (roles.HighlightedText, "text"), (roles.Link, "accent"),
        (roles.LinkVisited, "accent"),
    ):
        p.setColor(role, QColor(t[key]))
    disabled = QPalette.ColorGroup.Disabled
    for role in (roles.WindowText, roles.Text, roles.ButtonText, roles.PlaceholderText):
        p.setColor(disabled, role, QColor(t["text_disabled"]))
    p.setColor(disabled, roles.Highlight, QColor(t["hover"]))
    p.setColor(roles.Accent, QColor(t["accent"])) if hasattr(roles, "Accent") else None
    return p


def stylesheet(t: dict[str, str], base_pt: float) -> str:
    small = max(7.0, base_pt * 0.86)
    title = base_pt * 1.3
    big = base_pt * 1.8
    return f"""
* {{ outline: none; }}
QMainWindow, QDialog {{ background: {t['window']}; }}
QMainWindow::separator {{ background: {t['border']}; width: 1px; height: 1px; }}
QWidget#Panel, QWidget[role="panel"] {{ background: {t['panel']}; }}
QToolTip {{ background: {t['raised']}; color: {t['text']}; border: 1px solid {t['border_strong']};
    padding: 4px 6px; border-radius: 4px; }}

QMenuBar {{ background: {t['window']}; border-bottom: 1px solid {t['border']}; padding: 1px 4px; }}
QMenuBar::item {{ padding: 4px 9px; border-radius: 4px; background: transparent; }}
QMenuBar::item:selected {{ background: {t['hover']}; }}
QMenu {{ background: {t['raised']}; border: 1px solid {t['border_strong']}; padding: 4px; border-radius: 6px; }}
QMenu::item {{ padding: 5px 24px 5px 10px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['selection']}; }}
QMenu::item:disabled {{ color: {t['text_disabled']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 6px; }}
QMenu::icon {{ padding-left: 6px; }}

QToolBar {{ background: {t['window']}; border: none; border-bottom: 1px solid {t['border']};
    spacing: 2px; padding: 3px 6px; }}
QToolBar#ToolPalette {{ border-bottom: none; border-right: 1px solid {t['border']}; padding: 6px 4px; spacing: 4px; }}
QToolBar::separator {{ background: {t['border']}; width: 1px; height: 1px; margin: 4px 6px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px;
    color: {t['text']}; }}
QToolButton:hover {{ background: {t['hover']}; }}
QToolButton:pressed {{ background: {t['pressed']}; }}
QToolButton:checked {{ background: {t['accent_subtle']}; border-color: {t['accent_subtle']}; color: {t['accent']}; }}
QToolButton:focus {{ border-color: {t['accent']}; }}
QToolButton:disabled {{ color: {t['text_disabled']}; }}
QToolButton[popupMode="1"] {{ padding-right: 14px; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QPushButton {{ background: {t['raised']}; color: {t['text']}; border: 1px solid {t['border_strong']};
    border-radius: 6px; padding: 5px 14px; min-height: 18px; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:pressed {{ background: {t['pressed']}; }}
QPushButton:focus {{ border-color: {t['accent']}; }}
QPushButton:disabled {{ color: {t['text_disabled']}; border-color: {t['border']}; background: {t['panel']}; }}
QPushButton[variant="primary"] {{ background: {t['accent']}; color: {t['accent_text']}; border-color: {t['accent']};
    font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
QPushButton[variant="primary"]:focus {{ border-color: {t['text']}; }}
QPushButton[variant="primary"]:disabled {{ background: {t['hover']}; border-color: {t['hover']};
    color: {t['text_disabled']}; }}
QPushButton[variant="danger"] {{ color: {t['danger']}; }}
QPushButton[variant="flat"] {{ background: transparent; border-color: transparent; padding: 4px 8px; }}
QPushButton[variant="flat"]:hover {{ background: {t['hover']}; }}
QPushButton[variant="flat"]:focus {{ border-color: {t['accent']}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {t['input']}; color: {t['text']}; border: 1px solid {t['border_strong']}; border-radius: 6px;
    padding: 3px 6px; selection-background-color: {t['selection']}; selection-color: {t['text']}; }}
QPlainTextEdit, QTextEdit {{ padding: 4px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {t['accent']}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {t['text_disabled']}; border-color: {t['border']}; }}
QLineEdit[invalid="true"] {{ border-color: {t['danger']}; }}
QLineEdit[readOnly="true"] {{ background: {t['panel']}; }}
QComboBox {{ padding-right: 20px; }}
QComboBox QAbstractItemView {{ background: {t['raised']}; border: 1px solid {t['border_strong']};
    selection-background-color: {t['selection']}; padding: 2px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none; background: transparent; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {t['hover']}; }}

QCheckBox, QRadioButton {{ spacing: 6px; color: {t['text']}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {t['text_disabled']}; }}

QSlider::groove:horizontal {{ height: 4px; background: {t['border_strong']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {t['text']}; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QSlider::handle:horizontal:focus {{ background: {t['accent_hover']}; }}

QProgressBar {{ background: {t['input']}; border: 1px solid {t['border']}; border-radius: 4px; height: 8px;
    text-align: center; color: transparent; max-height: 8px; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle {{ background: {t['border_strong']}; border-radius: 4px; border: 2px solid transparent;
    background-clip: padding; }}
QScrollBar::handle:vertical {{ min-height: 28px; }}
QScrollBar::handle:horizontal {{ min-width: 28px; }}
QScrollBar::handle:hover {{ background: {t['text_faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QAbstractScrollArea::corner {{ background: transparent; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {t['border']}; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {t['text_muted']}; padding: 6px 12px; border: none;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {t['text']}; }}
QTabBar::tab:selected {{ color: {t['text']}; border-bottom-color: {t['accent']}; }}
QTabBar::tab:focus {{ color: {t['accent']}; }}

QDockWidget {{ color: {t['text_muted']}; font-weight: 600; }}
QDockWidget::title {{ background: {t['window']}; padding: 6px 8px; border-bottom: 1px solid {t['border']};
    text-align: left; }}
QDockWidget > QWidget {{ background: {t['panel']}; }}

QStatusBar {{ background: {t['window']}; border-top: 1px solid {t['border']}; color: {t['text_muted']}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {t['text_muted']}; padding: 0 6px; }}

QSplitter::handle {{ background: {t['border']}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QHeaderView::section {{ background: {t['panel']}; color: {t['text_muted']}; border: none;
    border-bottom: 1px solid {t['border']}; padding: 4px 6px; font-weight: 600; }}
QTableView, QTreeView, QListView {{ background: {t['input']}; alternate-background-color: {t['panel']};
    border: 1px solid {t['border']}; border-radius: 6px; gridline-color: {t['border']};
    selection-background-color: {t['selection']}; selection-color: {t['text']}; }}
QTableView::item, QTreeView::item, QListView::item {{ padding: 3px 4px; }}
QListView#NavList {{ background: transparent; border: none; }}
QListView#NavList::item {{ padding: 8px 10px; border-radius: 6px; margin: 1px 0; color: {t['text_muted']}; }}
QListView#NavList::item:hover {{ background: {t['hover']}; color: {t['text']}; }}
QListView#NavList::item:selected {{ background: {t['accent_subtle']}; color: {t['text']}; }}

QLabel[role="title"] {{ font-size: {title:.1f}pt; font-weight: 600; color: {t['text']}; }}
QLabel[role="display"] {{ font-size: {big:.1f}pt; font-weight: 600; color: {t['text']}; }}
QLabel[role="section"] {{ font-size: {small:.1f}pt; font-weight: 700; color: {t['text_faint']};
    letter-spacing: 0.6px; text-transform: uppercase; }}
QLabel[role="muted"] {{ color: {t['text_muted']}; }}
QLabel[role="faint"] {{ color: {t['text_faint']}; font-size: {small:.1f}pt; }}
QLabel[role="key"] {{ color: {t['text_muted']}; }}
QLabel[role="value"] {{ color: {t['text']}; }}

QFrame[role="segmented"] {{ background: {t['input']}; border: 1px solid {t['border']}; border-radius: 7px; }}
QFrame[role="segmented"] QToolButton {{ border: 1px solid transparent; border-radius: 5px; padding: 3px 12px;
    color: {t['text_muted']}; }}
QFrame[role="segmented"] QToolButton:hover {{ color: {t['text']}; background: transparent; }}
QFrame[role="segmented"] QToolButton:checked {{ background: {t['raised']}; color: {t['text']};
    border-color: {t['border_strong']}; }}
QFrame[role="segmented"] QToolButton:focus {{ border-color: {t['accent']}; }}

QFrame[role="card"] {{ background: {t['raised']}; border: 1px solid {t['border']}; border-radius: 8px; }}
QFrame[role="card"] QLabel {{ background: transparent; }}
QFrame[role="divider"] {{ background: {t['border']}; max-height: 1px; min-height: 1px; border: none; }}
QFrame[role="dropzone"] {{ background: {t['input']}; border: 1.5px dashed {t['border_strong']}; border-radius: 10px; }}
QFrame[role="dropzone"][active="true"] {{ border-color: {t['accent']}; background: {t['accent_subtle']}; }}

QLabel[pill] {{ border-radius: 9px; padding: 1px 9px; font-weight: 600; font-size: {small:.1f}pt; }}
QLabel[pill="ok"] {{ background: {t['success_subtle']}; color: {t['success']}; }}
QLabel[pill="warn"] {{ background: {t['warning_subtle']}; color: {t['warning']}; }}
QLabel[pill="error"] {{ background: {t['danger_subtle']}; color: {t['danger']}; }}
QLabel[pill="info"] {{ background: {t['accent_subtle']}; color: {t['accent']}; }}
QLabel[pill="neutral"] {{ background: {t['hover']}; color: {t['text_muted']}; }}

QFrame[banner] {{ border-radius: 8px; border: 1px solid {t['border']}; }}
QFrame[banner="ok"] {{ background: {t['success_subtle']}; border-color: {t['success_subtle']}; }}
QFrame[banner="warn"] {{ background: {t['warning_subtle']}; border-color: {t['warning_subtle']}; }}
QFrame[banner="error"] {{ background: {t['danger_subtle']}; border-color: {t['danger_subtle']}; }}
QFrame[banner="info"] {{ background: {t['accent_subtle']}; border-color: {t['accent_subtle']}; }}
QFrame[banner] QLabel {{ background: transparent; }}
QFrame[banner="ok"] QLabel[role="title"] {{ color: {t['success']}; }}
QFrame[banner="warn"] QLabel[role="title"] {{ color: {t['warning']}; }}
QFrame[banner="error"] QLabel[role="title"] {{ color: {t['danger']}; }}
"""


def apply_theme(app, name: str = "dark") -> None:
    """Fusion style + token palette + generated stylesheet. Safe to call again to switch."""
    global _theme_name
    if name not in THEMES:
        name = "dark"
    _tokens.clear()
    _tokens.update(THEMES[name])
    _theme_name = name
    _render_icon.cache_clear()
    app.setStyle("Fusion")
    app.setPalette(_palette(_tokens))
    base = app.font().pointSizeF()
    app.setStyleSheet(stylesheet(_tokens, base if base > 0 else 10.0))


def repolish(widget) -> None:
    """Re-apply the stylesheet after changing a dynamic property used by a selector."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_role(widget, role: str):
    widget.setProperty("role", role)
    return widget


def set_pill(label, kind: str, text: str | None = None) -> None:
    """Status pill: kind is ok | warn | error | info | neutral."""
    label.setProperty("pill", kind)
    if text is not None:
        label.setText(text)
    repolish(label)


# ----------------------------------------------------------------------------- icons

_STROKE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" '
           'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')

ICONS: dict[str, str] = {
    # files and documents
    "open": '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15'
            'A1.5 1.5 0 0 1 3 18.5z"/>',
    "folder": '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15'
              'A1.5 1.5 0 0 1 3 18.5z"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "text": '<path d="M5 7V4.5h14V7"/><path d="M12 4.5v15"/><path d="M9 19.5h6"/>',
    "save": '<path d="M4 5a1 1 0 0 1 1-1h11l4 4v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"/><path d="M8 4v4h7V4"/>'
            '<path d="M7.5 20v-6h9v6"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="1.5"/><circle cx="8.5" cy="9.5" r="1.5"/>'
             '<path d="m21 16-5-5L5 20"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="1.5"/>'
            '<path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>',
    "paste": '<rect x="5" y="4.5" width="14" height="16" rx="1.5"/><path d="M9 4.5V3h6v1.5"/><path d="M9 11h6M9 15h4"/>',
    "reveal": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/>'
              '<path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
    # history
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "redo": '<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>',
    "history": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6L3.5 8.5"/><path d="M3.5 3.5v5h5"/><path d="M12 7.5V12l3 2"/>',
    # view
    "zoom-in": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/><path d="M11 8v6M8 11h6"/>',
    "zoom-out": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/><path d="M8 11h6"/>',
    "zoom": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    "zoom-fit": '<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5'
                'a1 1 0 0 1-1-1v-4"/>',
    "zoom-100": '<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5'
                'a1 1 0 0 1-1-1v-4"/><rect x="10" y="10" width="4" height="4" fill="{c}" stroke="none"/>',
    "grid": '<rect x="4" y="4" width="16" height="16" rx="1"/><path d="M9.33 4v16M14.67 4v16M4 9.33h16M4 14.67h16"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "split": '<rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M12 2.5v19"/>'
             '<path d="M4.5 4H12v16H4.5A1.5 1.5 0 0 1 3 18.5v-13A1.5 1.5 0 0 1 4.5 4z" fill="{c}" fill-opacity=".3"'
             ' stroke="none"/>',
    "highlight": '<rect x="4" y="4" width="16" height="16" rx="1"/>'
                 '<rect x="7.5" y="7.5" width="3.5" height="3.5" fill="{c}" stroke="none"/>'
                 '<rect x="13" y="13" width="3.5" height="3.5" fill="{c}" stroke="none"/>',
    "compare": '<rect x="3" y="3" width="12" height="12" rx="1.5"/><rect x="9" y="9" width="12" height="12" rx="1.5"/>',
    "hex": '<path d="M8 4H6.5A1.5 1.5 0 0 0 5 5.5V10l-2 2 2 2v4.5A1.5 1.5 0 0 0 6.5 20H8"/>'
           '<path d="M16 4h1.5A1.5 1.5 0 0 1 19 5.5V10l2 2-2 2v4.5a1.5 1.5 0 0 1-1.5 1.5H16"/>',
    "sliders": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/>'
               '<circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    "layout": '<rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M8 4v16M3 15h18"/>',
    # tools
    "inspect": '<rect x="9.5" y="9.5" width="5" height="5" rx=".5"/><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/>',
    "pencil": '<path d="M16.2 3.8a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z"/><path d="m14.2 5.8 3 3"/>',
    "eyedropper": '<path d="M15.5 4.5a2.1 2.1 0 0 1 3 0l1 1a2.1 2.1 0 0 1 0 3L17 11l-4-4z"/><path d="m12 6 6 6"/>'
                  '<path d="M14 9l-8 8v3h3l8-8"/>',
    "select": '<rect x="4" y="4" width="16" height="16" rx="1" stroke-dasharray="3 2.6"/>',
    "fill": '<rect x="4" y="4" width="16" height="16" rx="1" stroke-dasharray="3 2.6"/>'
            '<rect x="8" y="8" width="8" height="8" rx=".5" fill="{c}" stroke="none"/>',
    "hand": '<path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V11"/><path d="M11 10.5v-6a1.5 1.5 0 0 1 3 0V11"/>'
            '<path d="M14 10.5V6a1.5 1.5 0 0 1 3 0v5"/>'
            '<path d="M17 9.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7h-1a7 7 0 0 1-5.6-2.8l-2.8-3.6a1.6 1.6 0 0 1 2.5-2L8 15"/>',
    "goto": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',
    # transforms
    "rotate-cw": '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>',
    "rotate-ccw": '<path d="M4 12a8 8 0 1 0 2.34-5.66"/><path d="M4 4v5h5"/>',
    "rotate-180": '<path d="M4.5 11a7.5 7.5 0 0 1 14-3.5"/><path d="M19 3v4.5h-4.5"/><path d="M19.5 13a7.5 7.5 0 0 1-14 3.5"/>'
                  '<path d="M5 21v-4.5h4.5"/>',
    "flip-h": '<path d="M12 3v18" stroke-dasharray="2 2.2"/><path d="M8.5 7 3.5 17h5z"/><path d="m15.5 7 5 10h-5z"/>',
    "flip-v": '<path d="M3 12h18" stroke-dasharray="2 2.2"/><path d="M7 8.5 17 3.5v5z"/><path d="m7 15.5 10 5v-5z"/>',
    # status
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16 10"/>',
    "x-circle": '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
    "alert": '<path d="M10.3 4.2 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z"/>'
             '<path d="M12 9.5v4M12 17v.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/>',
    "shield": '<path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6z"/>'
              '<path d="m8.5 12 2.5 2.5 4.5-5"/>',
    "shield-alert": '<path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6z"/>'
                    '<path d="M12 8v5M12 16.5v.01"/>',
    "dot": '<circle cx="12" cy="12" r="4.5" fill="{c}" stroke="none"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-right": '<path d="m9 6 6 6-6 6"/>',
    "arrow-right": '<path d="M4 12h15"/><path d="m13 6 6 6-6 6"/>',
    # ImageStore workflow
    "encode": '<path d="M2.5 12h7.5"/><path d="m7 8.5 3.5 3.5L7 15.5"/><rect x="13" y="4" width="8" height="16" rx="1"/>'
              '<path d="M13 9.33h8M13 14.67h8M17 4v16"/>',
    "decode": '<rect x="3" y="4" width="8" height="16" rx="1"/><path d="M3 9.33h8M3 14.67h8M7 4v16"/>'
              '<path d="M14 12h7.5"/><path d="m18 8.5 3.5 3.5-3.5 3.5"/>',
    "verify": '<rect x="3" y="3" width="11" height="11" rx="1"/><path d="M3 8.5h11M8.5 3v11"/>'
              '<circle cx="16.5" cy="16.5" r="4"/><path d="m21.5 21.5-2-2"/>',
}

_FILLED_LOGOS = {
    # ImageStore: one RGBA pixel, four channels, all carrying data.
    "logo-imagestore": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect x="2" y="2" width="20" height="20" rx="5" fill="#1f2125"/>'
        '<rect x="5.5" y="5.5" width="5.8" height="5.8" rx="1.2" fill="#f07575"/>'
        '<rect x="12.7" y="5.5" width="5.8" height="5.8" rx="1.2" fill="#52c785"/>'
        '<rect x="5.5" y="12.7" width="5.8" height="5.8" rx="1.2" fill="#6aa5ff"/>'
        '<rect x="12.7" y="12.7" width="5.8" height="5.8" rx="1.2" fill="none" stroke="#b8bcc4" stroke-width="1.3"/>'
        '</svg>'),
    # eXact: a single pixel held exactly in a crosshair.
    "logo-exact": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect x="2" y="2" width="20" height="20" rx="5" fill="#1f2125"/>'
        '<rect x="9.5" y="9.5" width="5" height="5" rx=".8" fill="#5b93ff"/>'
        '<path d="M12 5v2.6M12 16.4V19M5 12h2.6M16.4 12H19" stroke="#e6e8eb" stroke-width="1.6" stroke-linecap="round"/>'
        '</svg>'),
}


def svg_markup(name: str, color_hex: str) -> str:
    if name in _FILLED_LOGOS:
        return _FILLED_LOGOS[name]
    body = ICONS[name].replace("{c}", color_hex)
    return _STROKE.format(c=color_hex, body=body)


@lru_cache(maxsize=1024)
def _render_icon(name: str, color_hex: str, w: int, h: int, scale: float) -> QPixmap:
    pm = QPixmap(max(1, round(w * scale)), max(1, round(h * scale)))
    pm.setDevicePixelRatio(scale)
    pm.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(svg_markup(name, color_hex).encode()))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, w, h))
    painter.end()
    return pm


class _SvgIconEngine(QIconEngine):
    """Renders an icon from SVG at whatever size and device pixel ratio Qt asks for, in the
    active theme's colours (so icons follow theme changes and stay crisp on HiDPI)."""

    def __init__(self, name: str, tone: str | None = None):
        super().__init__()
        self.name = name
        self.tone = tone

    def _color(self, mode: QIcon.Mode, state: QIcon.State) -> str:
        if self.tone:
            return _tokens.get(self.tone, self.tone)
        if mode == QIcon.Mode.Disabled:
            return _tokens["text_disabled"]
        if state == QIcon.State.On:
            return _tokens["accent"]
        if mode in (QIcon.Mode.Active, QIcon.Mode.Selected):
            return _tokens["text"]
        return _tokens["icon"]

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        scale = painter.device().devicePixelRatioF() if painter.device() else 1.0
        pm = _render_icon(self.name, self._color(mode, state), rect.width(), rect.height(), scale)
        painter.drawPixmap(rect, pm)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        return QPixmap(_render_icon(self.name, self._color(mode, state), size.width(), size.height(), float(scale)))

    def clone(self) -> "QIconEngine":
        return _SvgIconEngine(self.name, self.tone)


def icon(name: str, tone: str | None = None) -> QIcon:
    """Theme-coloured icon. *tone* pins a token colour (e.g. "success") for status icons."""
    if name not in ICONS and name not in _FILLED_LOGOS:
        raise KeyError(f"unknown icon {name!r}")
    return QIcon(_SvgIconEngine(name, tone))


def app_icon(name: str) -> QIcon:
    """Full-colour application mark ("logo-imagestore" or "logo-exact")."""
    return QIcon(_SvgIconEngine(name))


def swatch_pixmap(rgb: tuple[int, int, int], alpha: int | None, size: int, scale: float = 1.0) -> QPixmap:
    """A colour swatch: left half the raw RGB (alpha ignored), right half composited over a
    checkerboard when *alpha* is given. Display only."""
    pm = QPixmap(round(size * 2 * scale), round(size * scale))
    pm.setDevicePixelRatio(scale)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    r, g, b = rgb
    left = QRect(0, 0, size if alpha is not None else size * 2, size)
    p.fillRect(left, QColor(r, g, b))
    if alpha is not None:
        cell = max(3, size // 4)
        for yy in range(0, size, cell):
            for xx in range(size, size * 2, cell):
                dark = ((xx - size) // cell + yy // cell) & 1
                p.fillRect(QRect(xx, yy, cell, cell), QColor(_tokens["checker_dark" if dark else "checker_light"]))
        p.fillRect(QRect(size, 0, size, size), QColor(r, g, b, alpha))
    p.setPen(QColor(_tokens["border_strong"]))
    p.drawRect(QRect(QPoint(0, 0), QSize(size * 2 - 1, size - 1)))
    p.end()
    return pm
