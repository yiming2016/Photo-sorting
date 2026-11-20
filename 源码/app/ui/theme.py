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

# 网格/列表的鼠标悬浮与选中底色(v220): 对齐 Windows 资源管理器 ——
# 悬浮是浅蓝、选中再深一档, 两者一眼能分清"我只是划过"和"我真的勾上了"。
# ⚠️ 以前悬浮是灰(#F3F5F8)、选中是更浅的 ACCENT_SOFT, 对比反了。
HOVER_SOFT = "#E9F1FD"
SEL_SOFT = "#C8DCFA"
SEL_EDGE = "#9CBDF6"

OK = "#1F9254"
OK_SOFT = "#E8F6EE"
WARN = "#C2740A"
WARN_SOFT = "#FDF3E2"
DANGER = "#D93F3F"
DANGER_SOFT = "#FDEDED"

THUMB_BG = "#EDF0F4"
HEADER_BG = "#F7F9FC"

# Tooltip: 与下方 QToolTip QSS 里的值保持一致, 给需要自定义弹窗的控件用
TOOLTIP_BG = "#FFFFFF"
TOOLTIP_FG = "#1A1A1A"

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
/* ⚠️ 顶层窗口如果是**普通 QWidget**(比如剪辑的独立窗口), 上面两条都不匹配,
   只剩 `QWidget [background: transparent]` —— 窗口底就是透明的, 直接透出桌
   面/系统深色 → 看上去整个窗口变成"黑色主题"。`:window` 只挑顶层窗口, 子控件
   照旧 transparent, 所以这一条专治"独立窗口发黑"。 */
QWidget:window {{
    background: {BG};
}}
QToolTip {{
    background: {TOOLTIP_BG};
    color: {TOOLTIP_FG};
    border: 1px solid #D8D8D8;
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
/* 分段选择按钮(合并面板的「无损/转码」): 选中态用浅蓝底 + 蓝字 */
QPushButton[seg="true"] {{
    padding: 6px 10px;
}}
QPushButton[seg="true"]:checked {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT_BORDER};
    color: {ACCENT};
    font-weight: 600;
}}
QPushButton[seg="true"]:checked:hover {{
    background: #E3EDFF;
    border-color: {ACCENT};
}}
/* 顶栏形态切换滑块(排序/合并/统一): 灰轨道 + 细边 + 选中段实心蓝底白字。
   ⚠️ 不要写 min-width: 会和 padding 叠加把按钮撑宽, 反过来溢出轨道裁掉选中块(v211)。
   圆角: 轨道 9 = 内块 6 + 内边距 2 + 边框 1, 内外同心才不显脏。
   v212: 顶栏是纯白(#FFFFFF), 原来的 #F1F3F8 几乎看不出这是一条「框」;
   底色加深到 #E7EAF1 并补 1px #D5DBE5 细边, 明显但不至于压住里面的蓝块。 */
#modeSwitch {{
    background: #E7EAF1;
    border: 1px solid #D5DBE5;
    border-radius: 9px;
    padding: 2px;
}}
QPushButton#modeBtn {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0 13px;
    min-height: 22px;
    color: {TEXT_SUB};
    font-size: 12px;
}}
QPushButton#modeBtn:hover {{
    background: rgba(59, 107, 245, 0.09);
    color: {TEXT};
}}
QPushButton#modeBtn:checked {{
    background: {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton#modeBtn:checked:hover {{
    background: {ACCENT_HOVER};
}}
/* 合并面板顶部一行放「自动 + 无损 + 转码 + ⚙」四个, 定宽保证放得下 */
QPushButton[seg="true"][narrow="true"] {{
    min-width: 76px;
    max-width: 76px;
}}
QPushButton#mergeAutoBtn {{
    min-width: 56px;
    max-width: 56px;
}}
QPushButton#mergeGearBtn {{
    min-width: 34px;
    max-width: 34px;
    padding: 6px 2px;
    font-size: 15px;
}}
/* 「无损」板块的「全选」三态: full=全蓝 / part=半蓝 / 无属性=灰(默认) */
QPushButton[lossless="full"] {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton[lossless="full"]:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton[lossless="part"] {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT_BORDER};
    color: {ACCENT};
    font-weight: 600;
}}
QPushButton[lossless="part"]:hover {{
    background: #E3EDFF;
    border-color: {ACCENT};
}}
/* 「独立」板块的「全选」三态: full=全蓝 / part=半蓝 / 无属性=灰(默认) */
QPushButton[solo="full"] {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton[solo="full"]:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton[solo="part"] {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT_BORDER};
    color: {ACCENT};
    font-weight: 600;
}}
QPushButton[solo="part"]:hover {{
    background: #E3EDFF;
    border-color: {ACCENT};
}}

/* ---------- 剪辑工具条(图标在上、文字在下) ---------- */
QToolButton#tlTool {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    color: {TEXT_SUB};
    font-size: 11px;
    padding: 2px 0 3px 0;
}}
QToolButton#tlTool:hover {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT_BORDER};
    color: {ACCENT};
}}
QToolButton#tlTool:pressed {{
    background: #D8E6FF;
    border-color: {ACCENT};
}}
QToolButton#tlTool:disabled {{
    background: transparent;
    border-color: transparent;
    color: {TEXT_WEAK};
}}

/* ---------- 剪辑页顶部返回栏 ---------- */
QWidget#editTopBar {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
QPushButton#editBackBtn {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 5px 12px;
    color: {TEXT};
    font-size: 12px;
}}
QPushButton#editBackBtn:hover {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT};
    color: {ACCENT};
}}

/* ---------- 磁吸 / 吸附 开关(选中=蓝底) ---------- */
/* v150: 能点的就不用灰字 —— 灰字等于"点不了", 用户得先试着点一下才知道。
   只有 disabled 才灰, 可用的一律正常深色文字。 */
QPushButton#chipToggle {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 3px 12px;
    color: {TEXT};
    font-size: 11px;
}}
QPushButton#chipToggle:hover {{
    border-color: {ACCENT_BORDER};
    color: {ACCENT};
}}
QPushButton#chipToggle:checked {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT};
    color: {ACCENT};
    font-weight: 600;
}}
QPushButton#chipToggle:disabled {{
    color: {TEXT_WEAK};
    border-color: {BORDER};
}}
/* v188: 「组」只靠竖线分隔, 不给这一组套浅灰底框 */
/* 截图范围(当前/选中/所有): 三个值都是两个字, 内边距收窄才装得下 */
QPushButton#shotScope {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 3px 4px;
    color: {TEXT};
    font-size: 11px;
}}
QPushButton#shotScope:hover {{
    border-color: {ACCENT_BORDER};
    color: {ACCENT};
}}

/* ---------- 快捷键面板里的键位(可点着改) ---------- */
QPushButton#keyBtn {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 9px;
    color: {TEXT};
    font-size: 12px;
}}
QPushButton#keyBtn:hover {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton#keyBtn[capturing="true"] {{
    background: {ACCENT_SOFT};
    border: 1px dashed {ACCENT};
    color: {ACCENT};
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
/* 统一页顶栏的 4 个参数下拉(v213): 只收窄内边距与箭头区, 让框身贴着最长选项走。
   ⚠️ 宽度不写在这里 —— 由 norm_page._fit_combo_width() 按"最长选项文字 + 装饰"现算,
      换文案/改字体自动跟着变; QSS 里写死 min-width 会再次把框撑宽(v211 教训)。 */
QComboBox#normCombo {{
    padding: 6px 6px;
}}
QComboBox#normCombo::drop-down {{
    border: none;
    width: 16px;
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
/* ---------- 剪辑工具条上的截图区(v134) ---------- */
/* 相机图标按钮: 只有图标, 所以必须 padding:0(主题默认 6px 14px 会把 32px 挤成负数) */
QPushButton#shotBtn {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 0px;
}}
QPushButton#shotBtn:hover {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT};
}}
QPushButton#shotBtn:pressed {{
    background: {ACCENT_SOFT};
}}
/* 格式下拉: 要窄。min-width 必须显式压掉全局的 80px */
QComboBox#shotFmt {{
    min-width: 0px;
    padding: 0px 4px 0px 8px;
    font-size: 12px;
}}
QComboBox#shotFmt::drop-down {{
    width: 16px;
    /* 只留宽，不画背景/边框、也不拼 border 三角：
      · 写了 background/border → Qt 用它自己的盒子画，默认箭头彻底消失；
      · 用 border 拼三角（width/height 归零）→ 只会剩一个 1px 的小方块。 */
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
/* 空态页(还没放文件): 与「已经有文件」时的网格是同一张白卡 —— 同底色、同半径,
   同一圈 1px 淡边。不然一加文件/一清空, 主区就从白卡跳成灰底一行字, 像换了块区域(v212)。 */
QWidget#emptyPage {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
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
/* 选中行底色: 一旦给 ::item 写了 padding, 系统默认的选中高亮就不画了
   (树上只剩左侧缩进区一小块蓝)。必须显式给 item:selected 底色,
   配合视图的 SelectRows 行为, 选中才会铺满整行。 */
QTableView::item:selected, QTreeView::item:selected {{
    background: {ACCENT_SOFT};
    color: {TEXT};
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

/* ---------- 缩放滑轨(v169) ---------- */
QSlider#zoomSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER_STRONG};
    border-radius: 2px;
}}
QSlider#zoomSlider::sub-page:horizontal {{
    background: {ACCENT_BORDER};
    border-radius: 2px;
}}
QSlider#zoomSlider::handle:horizontal {{
    width: 12px;
    margin: -5px 0;
    border-radius: 6px;
    background: {ACCENT};
}}
QSlider#zoomSlider::handle:hover {{ background: {ACCENT_HOVER}; }}

/* 音量拖动条的样式写在 _VolumeSlider 里(挂在控件本身才压得住原生灰底) */
/* ---------- 分隔 ---------- */
QSplitter::handle {{
    background: {BORDER};
    width: 1px;
    height: 1px;
}}
QSplitter::handle:hover {{ background: {ACCENT_BORDER}; }}

/* ---------- 菜单 ---------- */
/* QMenu 也是顶层窗口, 会被上面 `QWidget:window` 抢成 {BG}; 这里用同特异性
   的 `QMenu:window` 压它(写在后面才生效), 保住菜单的纯白底。 */
QMenu, QMenu:window {{
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
/* 合并面板的进度条: 要能在条内显示百分比, 所以比默认的 8px 高一些 */
QProgressBar#mergeProgress {{
    min-height: 18px;
    max-height: 18px;
    border-radius: 9px;
    background: #E9EDF3;
    font-size: 11px;
    color: {TEXT_SUB};
}}
QProgressBar#mergeProgress::chunk {{
    border-radius: 9px;
    background: {ACCENT};
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
        # ⚠️ 路径里的反斜杠在 QSS 里是转义符(\U / \T 这种), 会让**整张样式表**
        #    解析失败 —— Qt 只打一句 "Could not parse application stylesheet",
        #    界面就等于裸奔。图片路径一律换成正斜杠。
        qss_path = radio_path.replace("\\", "/")
        QSS += f'\nQRadioButton::indicator:checked {{ image: url("{qss_path}"); }}'
    except Exception:
        pass

    app.setStyleSheet(QSS)
