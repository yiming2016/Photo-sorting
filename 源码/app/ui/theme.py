# -*- coding: utf-8 -*-
"""浅色主题: 统一的色彩常量与样式表。"""
from __future__ import annotations

# ---- 颜色 ----
BG = "#F4F6F9"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#FAFBFC"
BORDER = "#E3E7ED"
BORDER_STRONG = "#CDD4DE"

TEXT = "#1D2129"
TEXT_SUB = "#5F6B7A"
TEXT_WEAK = "#98A2B0"

ACCENT = "#3B6BF5"
ACCENT_HOVER = "#2F5BDC"
ACCENT_SOFT = "#EDF3FF"
ACCENT_BORDER = "#A9C1FB"

OK = "#1F9254"
OK_SOFT = "#E8F6EE"
WARN = "#C2740A"
WARN_SOFT = "#FDF3E2"
DANGER = "#D93F3F"
DANGER_SOFT = "#FDEDED"

THUMB_BG = "#EDF0F4"
HEADER_BG = "#F7F9FC"

# Tooltip: 与下方 QToolTip QSS 里的值保持一致, 给需要自定义弹窗的控件用
TOOLTIP_BG = "#2B313B"
TOOLTIP_FG = "#FFFFFF"

FONT_FAMILY = "'Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', 'Segoe UI', sans-serif"
MONO_FAMILY = "'Cascadia Mono', 'Consolas', 'Menlo', monospace"

from PySide6.QtGui import QFont

theme_font_mono = QFont(MONO_FAMILY, 11)

QSS = f"""
* {{
    font-family: {FONT_FAMILY};
    font-size: 13px;
    color: {TEXT};
}}
QWidget {{
    background: transparent;
}}
QMainWindow, QDialog {{
    background: {BG};
}}
QToolTip {{
    background: #2B313B;
    color: #FFFFFF;
    border: none;
    padding: 6px 9px;
    border-radius: 6px;
    font-size: 14px;
}}

/* ---------- 按钮 ---------- */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 6px 14px;
    min-height: 18px;
    color: {TEXT};
}}
QPushButton:hover {{
    background: #F2F5FA;
    border-color: #B9C3D2;
}}
QPushButton:pressed {{
    background: #E9EEF7;
}}
QPushButton:disabled {{
    color: {TEXT_WEAK};
    background: #F4F5F7;
    border-color: {BORDER};
}}
QPushButton[accent="true"] {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton[accent="true"]:disabled {{
    background: #B9C7F2;
    border-color: #B9C7F2;
    color: #FFFFFF;
}}
QPushButton[danger="true"] {{
    color: {DANGER};
    border-color: #F0BEBE;
}}
QPushButton[danger="true"]:hover {{
    background: {DANGER_SOFT};
}}
QPushButton[flat="true"] {{
    border: none;
    background: transparent;
    padding: 5px 9px;
}}
QPushButton[flat="true"]:hover {{
    background: #EDF0F5;
    border: none;
}}

/* ---------- 输入 ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QDateEdit {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus, QDateTimeEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit[readonly="true"] {{
    background: {SURFACE_ALT};
    color: {TEXT_SUB};
}}
QComboBox {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 6px 10px;
    min-width: 80px;
}}
QComboBox:hover {{ border-color: #B9C3D2; }}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    font-size: 13px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    padding: 5px 10px;
    border-radius: 4px;
}}
/* 鼠标悬浮 / 当前项的高亮: 不给规则的话(只给 ::item 的 padding 后),
   系统默认高亮就不再画了 —— 下拉菜单里会完全看不出选中项。 */
QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected {{
    background: {ACCENT_SOFT};
    color: {TEXT};
}}
QCheckBox, QRadioButton {{
    spacing: 7px;
    color: {TEXT};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    background: {SURFACE};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    border-radius: 3px;
    color: #FFFFFF;
}}
QRadioButton::indicator:checked {{
    border: 1px solid {ACCENT};
    background: {ACCENT};
    border-radius: 8px;
}}
QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_WEAK}; }}

/* ---------- 视图 ---------- */
QListView, QTreeView, QTableView {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    outline: none;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}
/* 顶栏/底栏: 用 ID 选择器只作用于 bar 本身, 不影响子控件 */
QWidget#topbar {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
QWidget#bottombar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
}}
QTableView::item, QTreeView::item {{
    padding: 4px 6px;
    border: none;
}}
QHeaderView::section {{
    background: {SURFACE_ALT};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 8px 10px;
    font-weight: 600;
    color: {TEXT_SUB};
}}
QTableWidget, QTableView {{
    gridline-color: {BORDER};
    alternate-background-color: {SURFACE_ALT};
}}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 2px 2px 2px 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 0 2px 2px 2px;
}}
QScrollBar::handle:vertical {{
    background: #CBD2DC;
    border-radius: 6px;
    min-height: 34px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: #CBD2DC;
    border-radius: 6px;
    min-width: 34px;
    margin: 2px;
}}
QScrollBar::handle:hover {{ background: #B3BCCA; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- 分隔 ---------- */
QSplitter::handle {{
    background: {BORDER};
    width: 1px;
    height: 1px;
}}
QSplitter::handle:hover {{ background: {ACCENT_BORDER}; }}

/* ---------- 菜单 ---------- */
QMenu {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 24px 7px 12px;
    border-radius: 6px;
    margin: 1px 2px;
}}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QMenu::item:disabled {{ color: {TEXT_WEAK}; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 5px 8px;
}}
QMenu::indicator {{
    width: 14px; height: 14px;
    left: 8px;
}}

/* ---------- 分组框 / 标签 ---------- */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    background: {SURFACE};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: {TEXT_SUB};
    font-weight: 600;
}}
QLabel[h2="true"] {{
    font-size: 14px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel[muted="true"] {{
    color: {TEXT_SUB};
}}
QLabel[weak="true"] {{
    color: {TEXT_WEAK};
    font-size: 12px;
}}
QLabel[mono="true"] {{
    font-family: {MONO_FAMILY};
    font-size: 12px;
}}

/* ---------- 进度条 ---------- */
QProgressBar {{
    background: #E9EDF3;
    border: none;
    border-radius: 5px;
    height: 8px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 5px;
}}

/* ---------- 状态栏 ---------- */
QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
    color: {TEXT_SUB};
}}
QStatusBar::item {{ border: none; }}

/* ---------- 标签页 ---------- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    background: {SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 7px 16px;
    margin-right: 4px;
    border-radius: 7px;
    color: {TEXT_SUB};
}}
QTabBar::tab:selected {{
    background: {SURFACE};
    color: {ACCENT};
    font-weight: 600;
}}
"""


def apply(app):
    import os
    import tempfile

    # 强制使用 Fusion 风格: Windows 原生风格会无视大量 QSS 自定义
    # (尤其是 QRadioButton / QCheckBox 的 indicator 背景), 导致自定义配色不生效。
    # Fusion 完整尊重 QSS, 保证主题按设计渲染。
    try:
        from PySide6.QtWidgets import QStyleFactory
        fusion = QStyleFactory.create("Fusion")
        if fusion is not None:
            app.setStyle(fusion)
    except Exception:
        pass

    # 生成蓝色实心圆 PNG（用于 QRadioButton 选中状态）
    # 注意: Windows 原生风格会无视 QSS 对 indicator 的背景色设置,
    # 因此必须用 image 贴图才能稳定显示蓝色填充。
    # QCheckBox 的勾选标记由 Fusion 原生绘制（通过 color: #FFFFFF 控制颜色），
    # 不需要额外 PNG 贴图。
    try:
        from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPixmap
        from PySide6.QtCore import QRectF, Qt
        # 生成蓝色实心圆 PNG（用于 QRadioButton 选中状态）
        rpix = QPixmap(16, 16)
        rpix.fill(Qt.transparent)
        r = QPainter(rpix)
        r.setRenderHint(QPainter.Antialiasing, True)
        rpath = QPainterPath()
        rpath.addEllipse(QRectF(0.5, 0.5, 15, 15))
        r.fillPath(rpath, QBrush(QColor(ACCENT)))
        r.end()
        radio_path = os.path.join(tempfile.gettempdir(), "photosort_radio.png")
        rpix.save(radio_path, "PNG")
        global QSS
        QSS += f'\nQRadioButton::indicator:checked {{ image: url("{radio_path}"); }}'
    except Exception:
        pass

    app.setStyleSheet(QSS)
