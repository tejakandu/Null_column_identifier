"""Look and feel: palette, stylesheet and the app icon (drawn in code)."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap

ACCENT = "#1F6FEB"
ACCENT_DARK = "#1858BC"
DANGER = "#C4314B"
SUCCESS = "#2E7D32"
WARNING = "#B26A00"
MUTED = "#6B7280"

STATUS_COLORS = {
    "ready": MUTED,
    "queued": MUTED,
    "running": ACCENT,
    "ok": SUCCESS,
    "nulls": WARNING,
    "error": DANGER,
    "cancelled": MUTED,
}

STYLESHEET = f"""
QMainWindow, QDialog {{ background: #F5F7FA; }}
QWidget {{ font-size: 10pt; color: #1F2328; }}
QLabel#title {{ font-size: 17pt; font-weight: 600; color: #0B2545; }}
QLabel#subtitle {{ color: {MUTED}; }}
QLabel#sectionTitle {{ font-size: 11pt; font-weight: 600; color: #0B2545; }}
QLabel#credits {{ color: {MUTED}; font-size: 8.5pt; }}
QLabel#hint {{ color: {MUTED}; font-size: 9pt; }}
QFrame#card {{
    background: white; border: 1px solid #E1E4E8; border-radius: 10px;
}}
QPushButton {{
    background: white; border: 1px solid #D0D7DE; border-radius: 6px;
    padding: 6px 12px; min-height: 18px;
}}
QPushButton:hover {{ background: #F3F4F6; border-color: #B8C0CA; }}
QPushButton:pressed {{ background: #E5E7EB; }}
QPushButton:disabled {{ color: #A0A7B0; background: #F6F8FA; border-color: #E1E4E8; }}
QPushButton#primary {{
    background: {ACCENT}; color: white; border: 1px solid {ACCENT_DARK};
    font-weight: 600; padding: 8px 22px; font-size: 11pt;
}}
QPushButton#primary:hover {{ background: {ACCENT_DARK}; }}
QPushButton#primary:disabled {{ background: #9DBEF5; border-color: #9DBEF5; color: white; }}
QPushButton#danger {{ color: {DANGER}; }}
QPushButton#success {{ color: {SUCCESS}; font-weight: 600; border-color: #9CCB9F; }}
QPushButton#link {{ border: none; background: transparent; color: {ACCENT}; padding: 2px 4px; }}
QPushButton#link:hover {{ text-decoration: underline; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
    background: white; border: 1px solid #D0D7DE; border-radius: 6px; padding: 3px 7px;
    min-height: 22px; selection-background-color: {ACCENT};
}}
QPlainTextEdit {{ padding: 6px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QTableView, QTreeWidget, QTreeView {{
    background: white; border: 1px solid #E1E4E8; border-radius: 6px;
    alternate-background-color: #FAFBFC; gridline-color: #EEF0F2;
    selection-background-color: #DCEAFE; selection-color: #0B2545;
}}
QTreeView::item, QListView::item {{ padding: 4px 4px; }}
QTreeView::item:hover, QListView::item:hover {{ background: #F1F5FD; }}
QTreeView::item:selected, QListView::item:selected {{ background: #DCEAFE; color: #0B2545; }}
QHeaderView::section {{
    background: #F6F8FA; border: none; border-bottom: 1px solid #E1E4E8;
    border-right: 1px solid #EEF0F2; padding: 6px; font-weight: 600; color: #374151;
}}
QTabWidget::pane {{ border: 1px solid #E1E4E8; border-radius: 8px; background: white; top: -1px; }}
QTabBar::tab {{
    background: transparent; padding: 7px 16px; margin-right: 2px; color: {MUTED};
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: #0B2545; border-bottom: 2px solid {ACCENT}; font-weight: 600; }}
QProgressBar {{
    border: none; background: #E5E7EB; border-radius: 4px; height: 8px; text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QRadioButton, QCheckBox {{ spacing: 7px; }}
QToolTip {{ background: #0B2545; color: white; border: none; padding: 5px; }}
QSplitter::handle {{ background: transparent; }}
"""


def app_font() -> QFont:
    f = QFont("Segoe UI")
    f.setPointSize(10)
    return f


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw_icon(size))
    return icon


def _draw_icon(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(0, 0, size, size).adjusted(size * 0.04, size * 0.04, -size * 0.04, -size * 0.04)
    path = QPainterPath()
    path.addRoundedRect(r, size * 0.2, size * 0.2)
    p.fillPath(path, QColor(ACCENT))
    # Three table columns, the middle one hollow (= null column).
    w = r.width() * 0.17
    gap = r.width() * 0.08
    left = r.left() + (r.width() - 3 * w - 2 * gap) / 2
    top, height = r.top() + r.height() * 0.22, r.height() * 0.56
    for i in range(3):
        col = QRectF(left + i * (w + gap), top, w, height)
        if i == 1:
            p.setPen(QPen(QColor("white"), max(1.0, size * 0.045)))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(col, w * 0.25, w * 0.25)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("white"))
            p.drawRoundedRect(col, w * 0.25, w * 0.25)
    p.end()
    return pm
