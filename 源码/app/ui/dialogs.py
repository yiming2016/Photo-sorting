# -*- coding: utf-8 -*-
"""各类对话框: 自动间隔、变更清单、回滚、设置。"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

from PySide6.QtCore import (QEvent, QItemSelectionModel, QObject,
                           QPoint, QPointF, QRect, QRectF, Qt, Signal, QTimer)
from PySide6.QtGui import (QColor, QBrush, QFont, QFontMetrics, QIcon, QKeyEvent,
                           QPainter, QPainterPath, QPalette, QPixmap, QPolygon, QPen)
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication,
                               QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QGridLayout, QLineEdit, QListWidget, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStyle,
                               QStyledItemDelegate,
                               QStyleOptionComboBox, QStyleOptionViewItem,
                               QTableWidget, QTableWidgetItem,
                               QTextEdit, QToolTip, QVBoxLayout, QWidget,
                               QWidgetAction)

from . import theme as T
from . import compare_model
from .widgets import (CheckCheckBox, fit_popup_width, prepare_combo_popup,
                      split_name_stem)
from ..core import backup, timeplan
from ..core.photo import Photo
from ..merge import grouping, naming
from ..merge import settings as merge_settings


# 永久删除弹窗里"鼠标悬浮文件名"弹出的略缩图边长(px)
THUMB_PREVIEW = 200

_DT_SEGMENT_QSS = """
/* 选中段整段蓝色背景白字, 无闪烁光标。无外框: 起始时间的 6 段看上去只是数字, 不要外面那圈框。*/
QSpinBox {
    selection-background-color: """ + T.ACCENT + """;
    selection-color: #FFFFFF;
    padding: 0px;
    background: transparent;
    border: none;
}
QSpinBox:focus {
    background: transparent;
    border: none;
}
QSpinBox::up-button, QSpinBox::down-button { width: 0; height: 0; }
"""


def _hide_spinbox_cursor(spin: QSpinBox):
    """隐藏 QSpinBox 内置 QLineEdit 的闪烁光标, 并关闭右键上下文菜单。"""
    le = spin.findChild(QLineEdit)
    if le is not None:
        le.setProperty("cursorWidth", 0)         # 隐藏闪烁光标竖线
        le.setContextMenuPolicy(Qt.NoContextMenu)


class _CenterAlignDelegate(QStyledItemDelegate):
    """让 QComboBox 下拉项文字水平居中, 并补画"悬浮/当前选中"的标记。

    - 居中: 只覆写 initStyleOption 把 displayAlignment 写死 AlignCenter, 渲染走基类。
    - 悬浮高亮: 全局 QSS 给 item 设了 padding/border-radius 会让系统默认 hover 高亮消失,
      这里自绘一层浅蓝圆角底。
    - 当前选中项: 直接比对 combo.currentIndex() 与该项行号(比依赖 State_HasFocus 更稳——
      弹窗里当前项未必带 HasFocus 状态), 只铺一层浅蓝底做标记; v214 起不再画对勾
      (勾会占掉结尾文字的位置, 用户明确不要)。

    combo: 所归属的 QComboBox, 用于判定当前选中项(构造时传入, 见 _CenterCombo)。
    """

    def __init__(self, combo=None, parent=None):
        super().__init__(parent)
        self._combo = combo
        # v215: 用这个委托的 combo 一律让弹层只有一层圆角底(否则"外方内圆")
        if combo is not None:
            prepare_combo_popup(combo)

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignCenter

    def paint(self, painter, option, index):
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        current = False
        if self._combo is not None:
            try:
                current = (self._combo.currentIndex() == index.row())
            except Exception:
                current = False
        # v214: 当前项只留浅蓝底, 不再在右侧画对勾 —— 对勾会占掉最后一段字的位置
        # (v213 把控件收到"刚好放下最长文案"后, 勾几乎压在末字上)。
        if hovered or selected or current:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(T.ACCENT_SOFT))
            painter.drawRoundedRect(QRect(option.rect).adjusted(2, 1, -2, -1), 6, 6)
            painter.restore()
            option.state &= ~(QStyle.StateFlag.State_MouseOver
                              | QStyle.StateFlag.State_Selected)
        super().paint(painter, option, index)


class _ComboHighlightDelegate(QStyledItemDelegate):
    """左对齐下拉的浅蓝高亮(v261): 当前项 / 悬浮项各铺一层圆角底。

    与主界面 _CenterAlignDelegate 完全同款配色与判定逻辑, 只是不改文字对齐 ——
    统一参数弹窗里的选项大多较长, 左对齐才读得快。全局 QSS 的
    `QWidget { background: transparent }` 会让系统默认 selected 底色失效,
    所以这里必须自绘(和主界面顶栏下拉踩过同一个坑)。
    """

    def __init__(self, combo=None, parent=None):
        super().__init__(parent)
        self._combo = combo

    def paint(self, painter, option, index):
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        current = False
        if self._combo is not None:
            try:
                current = self._combo.currentIndex() == index.row()
            except Exception:
                current = False
        if hovered or selected or current:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(T.ACCENT_SOFT))
            painter.drawRoundedRect(QRect(option.rect).adjusted(2, 1, -2, -1), 6, 6)
            painter.restore()
            # 画完自己的底色后清掉状态, 免得基类再盖一层系统高亮/白底
            option.state &= ~(QStyle.StateFlag.State_MouseOver
                              | QStyle.StateFlag.State_Selected)
        super().paint(painter, option, index)


class _ModeCombo(QComboBox):
    """「合并方式」下拉（无损 / 转码），v105 重做。

    解决三件事：
    1. **只列没选中的那一项**：一共就两个选项，选了的那个再列一遍没意义；
    2. **文字真居中**：自绘 + 按字体真实墨迹对齐整框几何中心（不是按字宽），
       之前用 QSS `color`/`padding` 时文字被右侧 16px 的下拉区顶偏；
    3. **右侧不被裁**：自绘圆角边框 + 右边缘内侧的小三角，三角不占布局宽度。
       宽度由控件自己 `setFixedSize` 定死，表格列不会再把它裁掉一截
       （之前靠 QSS `max-width` + `ResizeToContents` 列宽，列按表头文字算宽度 → 右边被切）。
    """

    W = 60
    H = 22

    def __init__(self, items=("无损", "转码"), parent=None):
        super().__init__(parent)
        self.setObjectName("mergeModeCombo")
        self.setCursor(Qt.PointingHandCursor)
        for t in items:
            self.addItem(t)
        # 主题里 `QComboBox { min-width: 80px }` 比 setFixedSize 更权威，会把框撑到 102px
        # 又被表格列裁掉右边。用带 objectName 的局部 QSS 定死尺寸，并清掉自身的底/框
        # （框和背景都由 paintEvent 自己画）。
        self.setStyleSheet(
            f"#mergeModeCombo {{ min-width: {self.W}px; max-width: {self.W}px;"
            f" min-height: {self.H}px; max-height: {self.H}px;"
            f" background: transparent; border: none; padding: 0px; }}")
        self.setFixedSize(self.W, self.H)
        self.currentIndexChanged.connect(lambda _i: self.update())

    # ---------------------------------------------------------------- 颜色
    def _color(self, i: int | None = None) -> str:
        i = self.currentIndex() if i is None else i
        return T.OK if i == 0 else T.DANGER

    # ---------------------------------------------------------------- 关闭态
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(T.BORDER), 1))
        p.setBrush(QColor(T.SURFACE))
        p.drawRoundedRect(rect, 6, 6)

        col = QColor(self._color())
        f = QFont(self.font())
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(col)
        text = self.currentText()
        tri_w, gap = 7.0, 5.0
        fm = QFontMetrics(f)
        ink = fm.tightBoundingRect(text) if text else QRect()
        ink_w = float(ink.width()) if text else 0.0
        # 「文字 + 小三角」整体居中：只把文字居中会让人感觉整块偏左/偏右。
        group_w = ink_w + (gap + tri_w if text else 0.0)
        x0 = (self.width() - group_w) / 2.0
        if text:
            p.drawText(QPointF(x0 - ink.x(),
                               (self.height() - ink.height()) / 2.0 - ink.y()), text)
        # 小三角(不占布局宽度，不把文字挤偏)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        cx = x0 + ink_w + gap + tri_w / 2.0 if text else self.width() - 9.0
        cy = self.height() / 2.0
        p.drawPolygon(QPolygon([QPoint(int(cx - 3), int(cy - 2)),
                                QPoint(int(cx + 3), int(cy - 2)),
                                QPoint(int(cx), int(cy + 2))]))
        p.end()

    # ---------------------------------------------------------------- 弹出列表
    def _other_index(self) -> int:
        """还没选中的那一项（只有两项，选了 A 就只剩 B）。"""
        return 1 if self.currentIndex() == 0 else 0

    def showPopup(self):
        """弹出列表只放"还没选中的那一项"。"""
        other = self._other_index()
        menu = QMenu(self)
        menu.setCursor(Qt.PointingHandCursor)   # 菜单项悬浮也是抓手
        menu.setStyleSheet(
            f"QMenu {{ background: {T.SURFACE}; border: 1px solid {T.BORDER};"
            f" border-radius: 6px; padding: 4px; }}"
            f"QMenu::item {{ color: {self._color(other)}; font-size: 12px;"
            f" padding: 5px 20px; border-radius: 4px; }}"
            f"QMenu::item:selected {{ background: {T.ACCENT_SOFT}; }}")
        act = menu.addAction(self.itemText(other))
        if menu.exec(self.mapToGlobal(QPoint(0, self.height() + 2))) is act:
            self.setCurrentIndex(other)


class _DownComboBox(QComboBox):
    """下拉列表永远在控件正下方弹出（不随可用空间自动改成向上/居中）。

    Qt 默认的 QComboBox 会在「控件离窗口底部太近、下方放不下整个列表」时把列表翻到
    控件上方显示，导致同一个框有时向下、有时向上，体感很乱。这里在 showPopup 后
    强行把弹层定位到控件底边之下，统一向下。

    (v93 从 picker_dialog 搬到这里, 供「分组合并」的「合并方式」下拉复用;
     picker_dialog 改为从这里 import, 行为一致。)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        prepare_combo_popup(self)      # v215: 弹层只留一层圆角底

    def showPopup(self):
        fit_popup_width(self)      # v215: 弹层要放得下最长选项
        super().showPopup()
        self._force_down()

    def _force_down(self):
        pv = self.view()
        if pv is not None:
            # 移动弹层容器（顶层 popup 窗口），而不是里面的 listview；
            # 移 listview 会被容器布局覆盖，弹层反而跑偏。
            pv.window().move(self.mapToGlobal(self.rect().bottomLeft()))
            # 兜底：Qt 某些版本会在后续事件里重新定位弹层（基于可用空间判定），
            # 单次 move 可能被它覆盖，下一次事件循环再补一次。
            QTimer.singleShot(0, self._force_down_once)

    def _force_down_once(self):
        pv = self.view()
        if pv is not None and pv.window().isVisible():
            pv.window().move(self.mapToGlobal(self.rect().bottomLeft()))


class _CenterCombo(QComboBox):
    """关闭态文字水平+垂直都居中的 QComboBox。

    - 弹出列表项居中: 由 setItemDelegate(_CenterAlignDelegate) 负责。
    - 关闭态(显示当前选中项, 如"秒")居中:
        v48~v52 用过 subControlRect/手动 rect("去掉右侧箭头再居中"), 但字相对**整框**
        永远偏左约 7px — 因为下拉箭头区天然占了右侧 16px, 文字区中心 ≠ 整框中心。
      v53 (方案 B): 干脆让整个 widget rect 都是文字区, 字在**整框几何中心**
      AlignCenter。右侧那个下拉箭头不参与布局, 由 paintEvent 在右边缘重叠画
      一个 7x4px 的小三角(视觉上仍是下拉箭头, 但不占布局宽度)。这样字真居中。

      (对比: 之前所有版本的字相对整框中心偏 -7 ~ -13 px, v53 改为整框居中
       后偏差 ≈ 0, 仅字体抗锯齿 1~2px 的视觉误差)
    """

    # 在右边线内侧重叠画的小三角, 不占布局宽度
    _ARROW_W = 10  # 三角绘制占的横向区(像素), 仅用于 paintEvent 定位
    _ARROW_H = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        # v215: 弹层只留一层圆角底(去掉 Qt 弹层容器的直角背景)
        prepare_combo_popup(self)
        # 打开弹出列表的鼠标跟踪: 否则鼠标划过期项时不会产生 hover 状态,
        # 委托里就画不出"悬浮高亮"(就绪见 _CenterAlignDelegate.paint)。
        self.view().setMouseTracking(True)
        # 用带 combo 引用的委托(用于判定并标记"当前选中项")
        self.setItemDelegate(_CenterAlignDelegate(self))

    def paintEvent(self, event):
        opt = QStyleOptionComboBox()
        opt.initFrom(self)
        opt.editable = False
        opt.currentIcon = QIcon()
        opt.iconSize = self.iconSize()
        # 关闭 QComboBox 自带的 ::drop-down 子控件(它的箭头占 16px 会顶走文字区)。
        # 我们手工在右边缘画小三角(不占布局宽度), 让整框都能用作文字区。
        opt.subControls = QStyle.SC_ComboBoxFrame  # 只画边框/背景, 不画 drop-down 箭头
        style = self.style()
        painter = QPainter(self)
        # 1. 用空文本画背景/边框(不要 drop-down 箭头)
        opt.currentText = ""
        style.drawComplexControl(QStyle.CC_ComboBox, opt, painter, self)
        # 2. 让"文字真实墨迹"居中于整框
        #    单纯 AlignCenter 是按字宽(advance)对齐, 而中文字形在字框内左右/上下
        #    留白不均, 按字宽居中看起来会偏左/偏下。这里用 tightBoundingRect 取
        #    实际着墨范围, 用墨迹中心对齐控件中心, 做到肉眼真正居中。
        text = self.currentText()
        if text:
            painter.setPen(self.palette().color(self.foregroundRole()))
            fm = painter.fontMetrics()
            ink = fm.tightBoundingRect(text)
            x = (self.width() - ink.width()) / 2.0 - ink.x()
            y = (self.height() - ink.height()) / 2.0 - ink.y()
            painter.drawText(QPointF(x, y), text)
        painter.end()

    def showPopup(self):
        """弹出列表始终向下展开: 以控件底边左缘为锚点定位, 必要时仅在屏幕内夹紧, 不改变向下方向。"""
        fit_popup_width(self)      # v215: 弹层要放得下最长选项, 不能被省略号截断
        super().showPopup()
        container = self.view().parent() if self.view() is not None else None
        if container is None:
            return
        anchor = self.mapToGlobal(self.rect().bottomLeft())
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else QApplication.primaryScreen().availableGeometry()
        w, h = container.width(), container.height()
        x = anchor.x()
        y = anchor.y()
        if x + w > avail.right():
            x = max(avail.left(), avail.right() - w)
        if y + h > avail.bottom():
            y = max(avail.top(), avail.bottom() - h)
        container.move(x, y)


def confirm_destructive(parent, title: str, text: str, *,
                        center: bool = False,
                        symmetric_buttons: bool = False) -> bool:
    """二次确认框: 左「否」右「是」，全中文、无任何 Y/N 英文，「是」按钮为红色。

    - 默认焦点落在「否」：按 Enter 或 Esc 都 = 否（安全默认，避免误删）。
    - 用于「从列表移除」「永久删除文件」等危险操作。
    - center=True 时提示文字居中显示（用于永久删除弹窗）。
    - symmetric_buttons=True 时「否 / 是」左右对称居中分布（「是」在右侧）。
    - text 支持富文本（<font> 等），因此默认按 RichText 解析。
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(420)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(14)
    msg = QLabel(text)
    msg.setWordWrap(True)
    msg.setTextFormat(Qt.RichText)
    if center:
        msg.setAlignment(Qt.AlignCenter)
    else:
        msg.setProperty("muted", True)
    lay.addWidget(msg, 1)

    row = QHBoxLayout()
    if symmetric_buttons:
        row.addStretch(1)
    no_btn = QPushButton("否")
    no_btn.setMinimumWidth(84)
    no_btn.setDefault(True)        # 默认按钮 = 否（Enter/Esc 均触发"否"）
    yes_btn = QPushButton("是")
    yes_btn.setMinimumWidth(84)
    yes_btn.setStyleSheet(
        f"background: {T.DANGER}; border: 1px solid {T.DANGER}; color: #FFFFFF;"
        f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
    yes_btn.setCursor(Qt.PointingHandCursor)
    row.addWidget(no_btn)
    row.addWidget(yes_btn)
    if symmetric_buttons:
        row.addStretch(1)
    lay.addLayout(row)

    dlg.setModal(True)
    no_btn.clicked.connect(dlg.reject)
    yes_btn.clicked.connect(dlg.accept)
    return dlg.exec() == QDialog.DialogCode.Accepted


def confirm_yes_no(parent, title: str, text: str, *,
                   center: bool = False, red_yes: bool = True,
                   default_yes: bool = False) -> bool:
    """通用「否 / 是」确认框: 「否」居左、「是」居右, 无英文 Y/N、无问号图标。

    - red_yes=True: 「是」按钮为红色(危险操作, 如永久删除)。
    - red_yes=False: 「是」按钮为主题蓝色(常规操作, 如清空列表)。
    - default_yes=False: 默认焦点落在「否」(Enter / Esc 都 = 否, 安全默认)。
      default_yes=True: 默认焦点落在「是」(用于"建议你这么干"的提问, 如改用无损合并)。
    - text 支持富文本。
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(420)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(14)
    msg = QLabel(text)
    msg.setWordWrap(True)
    msg.setTextFormat(Qt.RichText)
    if center:
        msg.setAlignment(Qt.AlignCenter)
    else:
        msg.setProperty("muted", True)
    lay.addWidget(msg, 1)

    row = QHBoxLayout()
    no_btn = QPushButton("否")
    no_btn.setMinimumWidth(84)
    yes_btn = QPushButton("是")
    yes_btn.setMinimumWidth(84)
    (yes_btn if default_yes else no_btn).setDefault(True)
    if red_yes:
        yes_btn.setStyleSheet(
            f"background: {T.DANGER}; border: 1px solid {T.DANGER}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
    else:
        yes_btn.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
    yes_btn.setCursor(Qt.PointingHandCursor)
    row.addWidget(no_btn)
    row.addStretch(1)
    row.addWidget(yes_btn)
    lay.addLayout(row)

    dlg.setModal(True)
    no_btn.clicked.connect(dlg.reject)
    yes_btn.clicked.connect(dlg.accept)
    return dlg.exec() == QDialog.DialogCode.Accepted


def prompt_text(parent, title: str, label: str, initial: str = "") -> str | None:
    """单行文字输入框(改片名/轨名用)。回车 = 确定, Esc = 取消, 取消了返回 None。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(420)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(12)
    tip = QLabel(label)
    tip.setProperty("muted", True)
    lay.addWidget(tip)
    ed = QLineEdit(initial or "")
    ed.selectAll()
    lay.addWidget(ed)

    row = QHBoxLayout()
    cancel_btn = QPushButton("取消")
    cancel_btn.setMinimumWidth(84)
    ok_btn = QPushButton("确定")
    ok_btn.setMinimumWidth(84)
    ok_btn.setDefault(True)
    ok_btn.setStyleSheet(
        f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
        f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
    ok_btn.setCursor(Qt.PointingHandCursor)
    row.addStretch(1)
    row.addWidget(cancel_btn)
    row.addWidget(ok_btn)
    lay.addLayout(row)

    dlg.setModal(True)
    cancel_btn.clicked.connect(dlg.reject)
    ok_btn.clicked.connect(dlg.accept)
    ed.returnPressed.connect(dlg.accept)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return ed.text()


class MoveToMergeDialog(QDialog):
    """「移动到...」确认框：目标位置输入框 + 选择位置按钮 + 是否按序号重命名。

    - 位置是一个可编辑输入框，右侧「选择位置」按钮弹目录选择器回填；
    - 下方 ✅ 勾选框：勾选时按 01_原名、02_原名… 重命名，取消勾选则保留原文件名；
    - 底部「取消 / 移动」，「移动」为蓝色主按钮。
    """

    def __init__(self, count: int, target_dir: str, mixed: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("移动到...")
        self.setMinimumWidth(500)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        info = QLabel(f"将 <b>{count}</b> 个文件移动到：")
        info.setWordWrap(True)
        lay.addWidget(info)

        # 位置输入框 + 选择位置按钮
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = QLineEdit(target_dir)
        self.path_edit.setMinimumWidth(320)
        path_row.addWidget(self.path_edit, 1)
        choose_btn = QPushButton("选择位置")
        choose_btn.setCursor(Qt.PointingHandCursor)
        choose_btn.clicked.connect(self._choose_dir)
        path_row.addWidget(choose_btn)
        lay.addLayout(path_row)

        # 是否按序号重命名
        self.rename_check = CheckCheckBox("按顺序重命名为 01_原名、02_原名…")
        self.rename_check.setChecked(True)
        lay.addWidget(self.rename_check)

        if mixed:
            warn = QLabel(
                f"所选文件无法<font color='{T.DANGER}'>无损</font>合并")
            warn.setTextFormat(Qt.RichText)
            warn.setWordWrap(True)
            lay.addWidget(warn)

        row = QHBoxLayout()
        cancel_btn = QPushButton("取消")
        cancel_btn.setMinimumWidth(84)
        move_btn = QPushButton("移动")
        move_btn.setMinimumWidth(84)
        move_btn.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        move_btn.setCursor(Qt.PointingHandCursor)
        move_btn.setDefault(True)
        row.addWidget(cancel_btn)
        row.addStretch(1)
        row.addWidget(move_btn)
        lay.addLayout(row)

        cancel_btn.clicked.connect(self.reject)
        move_btn.clicked.connect(self.accept)
        self.path_edit.setFocus()

    def _choose_dir(self):
        start = self.path_edit.text().strip() or os.path.expanduser("~")
        if not os.path.isdir(start):
            up = os.path.dirname(start)
            if os.path.isdir(up):
                start = up
        d = QFileDialog.getExistingDirectory(self, "选择目标位置", start)
        if d:
            self.path_edit.setText(d)

    def chosen_dir(self) -> str:
        return self.path_edit.text().strip()

    def rename_enabled(self) -> bool:
        return self.rename_check.isChecked()


def _photo_dt(p):
    """命名用的时间: 拍摄时间优先, 读不到退到文件时间(sort_dt), 保证不会有空值。"""
    if p is None:
        return None
    f = getattr(p, "sort_dt", None)
    if callable(f):
        try:
            dt = f()
            if dt is not None and dt.year > 1900:
                return dt
        except Exception:
            pass
    return p.taken_dt()


class _VarPanel(QWidget):
    """变量面板：主窗口里的**普通子控件**，绝不是独立窗口。

    为什么不用 Qt.Popup / QMenu：任何独立弹出窗口在 Windows 上都会成为「活动窗口」，
    输入框随之失去激活态 —— 光标(竖线)消失、键盘打不进字，这就是之前反复修不好的根因。
    做成子控件后它没有自己的焦点，点按钮时焦点始终留在输入框里，于是
    「键盘输入」和「点选变量」可以真正同时进行：打字时面板不收、点面板时光标不丢。
    """

    picked = Signal(str)

    def __init__(self, edit, parent=None):
        super().__init__(parent)
        self._edit = edit
        self.setObjectName("varPanel")
        # 子控件要画出 QSS 背景必须开这个属性
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._build()
        self.hide()

    def _build(self):
        self._body = QWidget()
        self._body.setObjectName("varBody")
        root = QVBoxLayout(self._body)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(3)
        for title, items in naming.CATEGORIES:
            lab = QLabel(title)
            lab.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 11px; font-weight: 600;")
            lab.setFocusPolicy(Qt.NoFocus)
            lab.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            root.addWidget(lab)
            grid = QGridLayout()
            grid.setSpacing(5)
            r = c = 0
            for token, short, desc in items:
                b = QPushButton(short)          # 按钮只显示中文, 用户看不到 %fn 这类符号
                b.setToolTip(f"{desc}　（插入 {token}）")
                b.setCursor(Qt.PointingHandCursor)
                b.setFocusPolicy(Qt.NoFocus)    # 关键: 点它不抢走输入框焦点
                b.setProperty("var", "true")
                b.clicked.connect(lambda _=False, t=token: self.picked.emit(t))
                grid.addWidget(b, r, c)
                c += 1
                if c >= 4:
                    c = 0
                    r += 1
            root.addLayout(grid)

        # 面板可能比窗口还高(变量多), 套一层滚动: 矮窗口里也能滚到最后一组按钮
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.verticalScrollBar().setFocusPolicy(Qt.NoFocus)
        self._scroll.setWidget(self._body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

        self.setStyleSheet(
            f"QWidget#varPanel {{ background: {T.SURFACE}; "
            f"border: 1px solid {T.BORDER_STRONG}; border-radius: 8px; }}"
            f"QWidget#varPanel QWidget#varBody, QWidget#varPanel QScrollArea {{ "
            f"background: transparent; border: none; }}"
            f"QWidget#varPanel QLabel {{ background: transparent; border: none; }}"
            f"QWidget#varPanel QPushButton {{ "
            f"background: {T.SURFACE_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 5px; padding: 3px 6px; color: {T.ACCENT}; font-size: 11px; }}"
            f"QWidget#varPanel QPushButton:hover {{ "
            f"background: {T.ACCENT_SOFT}; border-color: {T.ACCENT_BORDER}; }}")

    def fit(self, max_height: int):
        """按可用高度算面板尺寸: 空间不足就变矮出滚动条(绝不为了塞下滑到输入框上)。"""
        # 首次弹出时子控件还没应用样式表, 直接量 sizeHint 会偏大(420 vs 实际 301),
        # 必须先 polish + 重算布局, 否则面板会白白宽出一截、往左压到内容上。
        self.ensurePolished()
        self._body.ensurePolished()
        lay = self._body.layout()
        if lay is not None:
            lay.activate()
        hint = self._body.sizeHint()
        w = hint.width() + 2
        h_full = hint.height() + 2
        h = min(h_full, max(120, max_height))
        if h < h_full:
            w += 14        # 给竖向滚动条留位置
        self.resize(w, h)
        return w, h

    # ---- 显示期间: 点面板外收起, 窗口缩放/移动时收起 ----
    def showEvent(self, event):
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def hideEvent(self, event):
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.MouseButtonPress:
            try:
                gp = event.globalPosition().toPoint()
            except Exception:
                gp = event.globalPos()
            node = QApplication.widgetAt(gp)
            while node is not None:
                if node is self or node is self._edit:
                    return False        # 点面板 / 点输入框 —— 都保持打开
                node = node.parentWidget()
            self.hide()
        elif et in (QEvent.Resize, QEvent.Move) and obj is self.parentWidget():
            self.hide()
        return False


class TemplateEdit(QLineEdit):
    """命名模板输入框：显示的是**中文**，点输入框在右侧展开变量面板，同时照常手动输入。

    对外隔一层翻译：setText 时把内部写法翻成中文（%ia → 序号）显示给用户，
    template_text() 再把中文翻回内部写法（序号 → %ia）交给渲染。
    用户在框里看到的全是中文，永远不需要知道符号。

    面板是窗口内的子控件(不抢焦点)，所以弹出后光标仍在、可直接打字，
    打字时面板不会收起；点面板里的中文按钮插入变量，焦点也仍在输入框里。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(naming.to_display(text), parent)
        self.setPlaceholderText("点这里选变量，或直接输入文字")
        self._panel: _VarPanel | None = None

    def setText(self, text: str):
        super().setText(naming.to_display(text))

    def template_text(self) -> str:
        """输入框内容 → 内部写法（中文变量翻回符号）。"""
        return naming.from_display(self.text())

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._popup_panel()

    def _ensure_panel(self) -> _VarPanel:
        if self._panel is None:
            host = self.window()
            self._panel = _VarPanel(self, host)
            self._panel.picked.connect(self._on_pick)
        return self._panel

    def _popup_panel(self):
        panel = self._ensure_panel()
        host = panel.parentWidget()
        if host is None:
            return
        # 横向: 一律贴窗口最右侧(靠右站), 让开左边, 免得盖住输入框下面的「示例」行。
        # 纵向: 贴在输入框正下方; 下方实在放不下才翻到上方。
        # 任何时候都不许为了"塞下"而滑到输入框上 —— 那样会挡住正在打的字。
        top_left = self.mapTo(host, QPoint(0, self.height() + 3))
        above_y = self.mapTo(host, QPoint(0, 0)).y()
        below = host.height() - top_left.y() - 6
        above = above_y - 9
        if below >= 150:
            w, h = panel.fit(below)
            y = top_left.y()
        elif above >= 150:
            w, h = panel.fit(above)
            y = above_y - h - 3
        elif below >= above:
            w, h = panel.fit(below)
            y = top_left.y()
        else:
            w, h = panel.fit(above)
            y = above_y - h - 3
        x = host.width() - w - 4          # 贴最右侧
        if x < 4:
            x = 4                          # 窗口比面板还窄时只能靠左
        y = max(4, y)
        panel.move(x, y)
        panel.show()
        panel.raise_()

    def _on_pick(self, token: str):
        self.setFocus(Qt.OtherFocusReason)
        # 插进输入框的是中文(序号), 不是 %ia —— 用户只看中文, 符号由 template_text 还原
        self.insert(naming.TOKEN_TO_CN.get(token, token))
        if self._panel is not None:
            self._panel.hide()


class _SampleLink(QLabel):
    """可点击的「示例：xxx」行 —— 点它打开命名规则库。"""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setStyleSheet(f"color: {T.ACCENT}; font-size: 12px;")
        self.setToolTip("点击：从现成的命名规则里选一条")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)


_PREVIEW_DT = datetime(2024, 8, 15, 14, 30, 22)


def _preview(tpl: str) -> str:
    """规则效果预览：用固定样例渲染一遍。"""
    return naming.render(tpl, index=1, total=3, name="IMG_1234",
                         dt=_PREVIEW_DT, spec="h264_1080p")


class NamePresetDialog(QDialog):
    """命名规则库：列出所有现成规则（规则名 + 效果），选中一条即可套用，也能新增。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("命名规则")
        self.setMinimumSize(520, 420)
        self.resize(560, 520)
        self._chosen = ""

        root = QVBoxLayout(self)
        root.setSpacing(10)

        tip = QLabel("选一条规则，双击或点「使用这条」即可套用。")
        tip.setProperty("muted", True)
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["规则名", "效果"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(lambda *_: self._use())
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        add = QPushButton("新增规则…")
        add.setMinimumWidth(96)
        add.clicked.connect(self._add)
        row.addWidget(add)
        row.addStretch(1)
        use = QPushButton("使用这条")
        use.setMinimumWidth(96)
        use.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        use.setCursor(Qt.PointingHandCursor)
        use.setDefault(True)
        use.clicked.connect(self._use)
        row.addWidget(use)
        root.addLayout(row)

        self._reload()

    def _reload(self):
        rows = naming.all_presets()
        self.table.setRowCount(len(rows))
        for r, (name, tpl) in enumerate(rows):
            it = QTableWidgetItem(name)
            it.setData(Qt.UserRole, tpl)
            self.table.setItem(r, 0, it)
            eff = QTableWidgetItem(_preview(tpl) or "（空）")
            eff.setFlags(eff.flags() & ~Qt.ItemIsEditable)
            eff.setForeground(QBrush(QColor(T.TEXT_SUB)))
            self.table.setItem(r, 1, eff)
        if rows:
            self.table.selectRow(0)

    def _add(self):
        dlg = AddPresetDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, tpl = dlg.result_preset()
            naming.add_preset(name, tpl)
            self._reload()
            # 选中刚加的那条，方便直接点「使用这条」
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).text() == name:
                    self.table.selectRow(r)
                    break

    def _use(self):
        it = self.table.item(self.table.currentRow(), 0)
        if it is None:
            return
        self._chosen = it.data(Qt.UserRole) or ""
        self.accept()

    def chosen(self) -> str:
        return self._chosen


class AddPresetDialog(QDialog):
    """新增一条命名规则：起个名字 + 拼规则（点输入框选变量），实时看效果。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新增命名规则")
        self.setMinimumSize(540, 300)
        self.resize(560, 330)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(self._label("规则名"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：旅行_序号")
        name_row.addWidget(self.name_edit, 1)
        root.addLayout(name_row)

        tpl_row = QHBoxLayout()
        tpl_row.setSpacing(8)
        tpl_row.addWidget(self._label("命名规则"))
        self.tpl_edit = TemplateEdit("%date_%ia")
        tpl_row.addWidget(self.tpl_edit, 1)
        root.addLayout(tpl_row)

        self.sample = QLabel()
        self.sample.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        self.sample.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        root.addWidget(self.sample)
        root.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("添加")
        ok.setMinimumWidth(96)
        ok.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setDefault(True)
        row.addWidget(ok)
        root.addLayout(row)

        ok.clicked.connect(self._on_ok)
        self.tpl_edit.textChanged.connect(self._update_sample)
        self._update_sample()

    @staticmethod
    def _label(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        return lab

    def _update_sample(self):
        self.sample.setText("示例：" + (_preview(self.tpl_edit.template_text()) or "—"))

    def _on_ok(self):
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "新增命名规则", "请先给这条规则起个名字。")
            return
        if not self.tpl_edit.template_text().strip():
            QMessageBox.warning(self, "新增命名规则", "命名规则不能为空。")
            return
        self.accept()

    def result_preset(self):
        return (self.name_edit.text().strip(),
                self.tpl_edit.template_text().strip())


class GroupMergeDialog(QDialog):
    """「按组合并」确认框：顶部保存位置 + 文件命名模板，列表逐组展示。

    每行 = 组信息(段数·规格) | 合并名称(可编辑, 默认按模板自动生成) | 合并方式下拉。
    jobs() 返回 [{"photos","out","reencode","label"}, ...]。
    """

    def __init__(self, groups, default_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("按组合并")
        self.setMinimumSize(640, 520)
        self.resize(680, 620)     # 给变量面板留够展开空间
        self._groups = groups
        self._build(default_dir)

    def _build(self, default_dir: str):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # 保存位置(统一) —— 标签 + 输入框 + 选择按钮 同一行
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        dir_row.addWidget(self._row_label("保存位置"))
        self.dir_edit = QLineEdit(default_dir)
        dir_row.addWidget(self.dir_edit, 1)
        pick = QPushButton("选择…")
        pick.setFixedWidth(64)
        pick.clicked.connect(self._pick_dir)
        dir_row.addWidget(pick)
        root.addLayout(dir_row)

        # 文件命名模板(点输入框弹变量菜单, 也可手动输入; 各组名称由此自动生成)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(self._row_label("文件命名"))
        self.name_template_edit = TemplateEdit("合并_%ia")
        name_row.addWidget(self.name_template_edit, 1)
        root.addLayout(name_row)
        self.name_sample = _SampleLink()
        self.name_sample.clicked.connect(
            lambda: self._pick_preset(self.name_template_edit))
        root.addWidget(self.name_sample)

        # 表格
        self.table = QTableWidget(len(self._groups), 3)
        self.table.setHorizontalHeaderLabels(["组", "合并名称", "合并方式"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        # 第 2 列「合并方式」定宽: ResizeToContents 只按表头文字算宽度(约 56px),
        # 装不下 60px 的下拉框 → 右边被裁掉一截。改成固定宽度, 框完整显示。
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 96)
        self._names: list[QLineEdit] = []
        self._combos: list[QComboBox] = []
        for r, g in enumerate(self._groups):
            photos = g.get("photos") or []
            spec = g.get("spec") or {}
            desc = grouping.spec_desc(spec) if spec else "规格未知"
            info = QLabel(f"{len(photos)} 段 · {desc}")
            info.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
            self.table.setCellWidget(r, 0, info)
            name_edit = QLineEdit()
            self._names.append(name_edit)
            self.table.setCellWidget(r, 1, name_edit)
            combo = _ModeCombo()         # 无损 / 转码: 文字居中, 弹出只列另一项
            self._combos.append(combo)
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(4, 3, 4, 3)
            hl.addStretch(1)
            hl.addWidget(combo)
            hl.addStretch(1)
            self.table.setCellWidget(r, 2, holder)
        root.addWidget(self.table, 1)

        # 预填默认名称(随输出目录里的已有文件自动排开)
        self._fill_default_names()
        self._update_name_sample()
        self.name_template_edit.textChanged.connect(self._on_template_changed)

        row = QHBoxLayout()
        start = QPushButton("开始合并")
        start.setMinimumWidth(100)
        start.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        start.setCursor(Qt.PointingHandCursor)
        start.setDefault(True)
        row.addStretch(1)
        row.addWidget(start)
        root.addLayout(row)
        start.clicked.connect(self._on_ok)

    def _row_label(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        return lab

    def _pick_preset(self, edit):
        """点「示例」→ 规则库 → 选中后回填到这个输入框(自动转中文并刷新示例/名称)。"""
        dlg = NamePresetDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        tpl = dlg.chosen()
        if tpl:
            edit.setText(tpl)

    def _pick_dir(self):
        start = self.dir_edit.text().strip() or os.getcwd()
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹", start)
        if d:
            self.dir_edit.setText(d)

    def _with_ext(self, stem: str) -> str:
        return stem if os.path.splitext(stem)[1] else stem + ".mp4"

    def _next_name(self, dir_path: str, used: set) -> str:
        for i in range(1, 1000):
            name = f"合并_{i:03d}.mp4"
            if name in used:
                continue
            if dir_path and os.path.exists(os.path.join(dir_path, name)):
                continue
            return name
        return f"合并_{len(used) + 1:03d}.mp4"

    def _dedup_name(self, base: str, d: str, used: set) -> str:
        low = base.lower()
        if low not in used and not (d and os.path.exists(os.path.join(d, base))):
            return base
        stem, ext = os.path.splitext(base)
        for j in range(2, 1000):
            cand = f"{stem} ({j}){ext}"
            if cand.lower() not in used and not (d and os.path.exists(os.path.join(d, cand))):
                return cand
        return base

    def _fill_default_names(self):
        tpl = self.name_template_edit.template_text().strip()
        n = len(self._groups)
        d = self.dir_edit.text().strip()
        used = set()
        for i, (g, e) in enumerate(zip(self._groups, self._names), 1):
            if not tpl:
                base = self._next_name(d, used)
            else:
                photos = g.get("photos") or []
                p0 = photos[0] if photos else None
                dt = _photo_dt(p0)
                name = naming.strip_index(os.path.splitext(p0.name)[0]) if p0 else ""
                stem = naming.render(tpl, index=i, total=n, name=name, dt=dt,
                                     spec=naming.spec_text(g.get("spec") or {}))
                base = self._dedup_name(self._with_ext(stem or f"合并_{i:03d}"),
                                        d, used)
            used.add(base.lower())
            e.setText(base)

    def _on_template_changed(self):
        self._update_name_sample()
        self._fill_default_names()

    def _update_name_sample(self):
        g = self._groups[0] if self._groups else {}
        photos = g.get("photos") or []
        p0 = photos[0] if photos else None
        dt = _photo_dt(p0)
        if dt is None:
            dt = datetime(2024, 8, 15, 14, 30, 22)
        name = naming.strip_index(os.path.splitext(p0.name)[0]) if p0 else "IMG_1234"
        spec = naming.spec_text(g.get("spec") or {}) or "h264_1080p"
        stem = naming.render(self.name_template_edit.template_text(), index=1,
                             total=len(self._groups), name=name, dt=dt, spec=spec)
        self.name_sample.setText("示例：" + (self._with_ext(stem) if stem else "合并_01.mp4"))

    def _on_ok(self):
        names = [e.text().strip() for e in self._names]
        for i, n in enumerate(names):
            if not n:
                QMessageBox.warning(self, "按组合并", f"第 {i + 1} 组的合并名称不能为空。")
                return
            if not os.path.splitext(n)[1]:
                names[i] = n + ".mp4"
        seen = {}
        for n in names:
            low = n.lower()
            if low in seen:
                QMessageBox.warning(self, "按组合并", f"合并名称重复：{n}")
                return
            seen[low] = True
        for e, n in zip(self._names, names):
            e.setText(n)
        self.accept()

    def jobs(self) -> list:
        d = self.dir_edit.text().strip()
        out = []
        for g, e, c in zip(self._groups, self._names, self._combos):
            name = e.text().strip()
            if not os.path.splitext(name)[1]:
                name += ".mp4"
            out_path = os.path.join(d, name) if d else name
            out.append({
                "photos": g.get("photos") or [],
                "out": out_path,
                "reencode": c.currentIndex() == 1,
                "label": name,
            })
        return out


class GroupFolderDialog(QDialog):
    """「新建文件夹」确认框：文件夹命名模板 / 组内重命名模板 / 目标位置。

    文件夹命名、文件命名都用命名模板（占位符 + 文字），下方实时显示示例。
    """

    def __init__(self, groups, default_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建文件夹")
        # 窗口默认就给足高度: 变量面板有 400+ px 才展得开, 不至于一打开就得滚
        self.setMinimumSize(560, 520)
        self.resize(580, 580)
        self._groups = groups
        root = QVBoxLayout(self)
        root.setSpacing(9)

        info = QLabel(f"把 {len(groups)} 个组分别移入各自的子文件夹（组与组互不串）：")
        info.setWordWrap(True)
        info.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        root.addWidget(info)

        # ---- 文件夹命名: 标签和输入框同一行, 示例在下一行 ----
        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        folder_row.addWidget(self._row_label("文件夹命名"))
        self.name_edit = TemplateEdit("分组_%ia")
        folder_row.addWidget(self.name_edit, 1)
        root.addLayout(folder_row)
        self.folder_sample = _SampleLink()
        self.folder_sample.clicked.connect(lambda: self._pick_preset(self.name_edit))
        root.addWidget(self.folder_sample)

        # ---- 组内重命名: 勾选框和输入框同一行; 开启后才显示输入框和下面的示例 ----
        rename_row = QHBoxLayout()
        rename_row.setSpacing(8)
        self.rename_check = CheckCheckBox("组内重命名")
        self.rename_check.setChecked(True)
        rename_row.addWidget(self.rename_check)
        self.rename_edit = TemplateEdit("%ia_%fn")
        rename_row.addWidget(self.rename_edit, 1)
        root.addLayout(rename_row)
        self.file_sample = _SampleLink()
        self.file_sample.clicked.connect(lambda: self._pick_preset(self.rename_edit))
        root.addWidget(self.file_sample)

        # ---- 路径 ----
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        lab2 = self._row_label("路径")
        path_row.addWidget(lab2)
        self.path_edit = QLineEdit(default_dir)
        path_row.addWidget(self.path_edit, 1)
        pick = QPushButton("选择位置")
        pick.clicked.connect(self._pick_dir)
        path_row.addWidget(pick)
        root.addLayout(path_row)

        # 拉高窗口时富余全部给这里, 控件保持贴顶紧凑, 不会被拉成一堆大空隙
        root.addStretch(1)

        row = QHBoxLayout()
        ok = QPushButton("移动")
        ok.setMinimumWidth(84)
        ok.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setDefault(True)
        row.addStretch(1)
        row.addWidget(ok)
        root.addLayout(row)
        ok.clicked.connect(self.accept)

        self.rename_check.toggled.connect(self._on_rename_toggled)
        self.name_edit.textChanged.connect(self._update_samples)
        self.rename_edit.textChanged.connect(self._update_samples)
        self._on_rename_toggled(self.rename_check.isChecked())
        self._update_samples()

    def _row_label(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        return lab

    def _spec_text(self, g: dict) -> str:
        return naming.spec_text(g.get("spec") or {})

    def _first_sample(self):
        """第一个组的真实日期/名字/规格，取不到时用固定样例。"""
        g = self._groups[0] if self._groups else {}
        photos = g.get("photos") or []
        p0 = photos[0] if photos else None
        dt = _photo_dt(p0)
        name = naming.strip_index(os.path.splitext(p0.name)[0]) if p0 else "IMG_1234"
        spec = self._spec_text(g) or "h264_1080p"
        return dt, name, spec

    def _on_rename_toggled(self, on: bool):
        self.rename_edit.setVisible(on)
        self.file_sample.setVisible(on)

    def _update_samples(self):
        dt, name, spec = self._first_sample()
        if dt is None:
            dt = datetime(2024, 8, 15, 14, 30, 22)
        n = len(self._groups)
        self.folder_sample.setText(
            "示例：" + naming.render(self.name_edit.template_text(), index=1, total=n,
                                     name=name, dt=dt, spec=spec))
        self.file_sample.setText(
            "示例：" + naming.render(self.rename_edit.template_text(), index=1, total=3,
                                     name=name, dt=dt, spec=spec))

    def _pick_dir(self):
        start = self.path_edit.text().strip() or os.getcwd()
        d = QFileDialog.getExistingDirectory(self, "选择目标位置", start)
        if d:
            self.path_edit.setText(d)

    def _pick_preset(self, edit):
        """点「示例」→ 规则库 → 选中后回填(自动转中文并刷新示例)。"""
        dlg = NamePresetDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        tpl = dlg.chosen()
        if tpl:
            edit.setText(tpl)

    def folder_name(self) -> str:
        return self.name_edit.template_text().strip() or "分组_%ia"

    def rename_enabled(self) -> bool:
        return self.rename_check.isChecked()

    def rename_template(self) -> str:
        return self.rename_edit.template_text().strip() or "%ia_%fn"

    def base_dir(self) -> str:
        return self.path_edit.text().strip()


_LEVEL_OPTIONS = [("fatal", "必须一致"), ("warn", "建议一致"), (None, "仅供参考")]
_LEVEL_COLORS = {"fatal": T.DANGER, "warn": T.WARN, None: T.OK}


def _group_of(level) -> int:
    """分档 -> 组序号(用于排序/判同档): fatal=0, warn=1, 仅供参考=2。"""
    return 0 if level == "fatal" else 1 if level == "warn" else 2


def _sorted_by_group(rows: list[dict], desc: bool) -> list[dict]:
    """按分档组重排(组内保持原有相对顺序): desc=False 必须一致在前, True 仅供参考在前。"""
    order = [2, 1, 0] if desc else [0, 1, 2]
    out: list[dict] = []
    for g in order:
        out.extend(r for r in rows if _group_of(r["level"]) == g)
    return out


class _MenuLabel(QLabel):
    """⚙️ 分档下拉里的带色项(红/黄/绿), 带悬浮高亮, 点击回调。"""

    picked = Signal(object)

    def __init__(self, text: str, color: str, value, parent=None):
        super().__init__(text, parent)
        self._value = value
        self._base = f"color: {color}; padding: 6px 14px; font-size: 13px;"
        self._hover = self._base + f" background: {T.ACCENT_SOFT}; border-radius: 5px;"
        self.setStyleSheet(self._base)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def enterEvent(self, event):
        self.setStyleSheet(self._hover)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setStyleSheet(self._base)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.picked.emit(self._value)
        super().mouseReleaseEvent(event)


class _LevelCombo(QComboBox):
    """只读的「分档」下拉: 显示当前档(红/黄/绿), 点开只列出另外两档(同样带色)。"""

    changed = Signal(object)   # 发出新档位 ("fatal" / "warn" / None)

    def __init__(self, level, parent=None):
        super().__init__(parent)
        self._level = level
        self._menu: QMenu | None = None
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.addItem("")                 # 占位, 仅作显示
        self._refresh()

    def level(self):
        return self._level

    def _refresh(self):
        text = dict(_LEVEL_OPTIONS)[self._level]
        self.setItemText(0, text)
        color = _LEVEL_COLORS[self._level]
        self.setStyleSheet(f"QComboBox {{ color: {color}; }}")
        pal = self.palette()
        pal.setColor(QPalette.Text, QColor(color))
        pal.setColor(QPalette.ButtonText, QColor(color))
        self.setPalette(pal)

    def showPopup(self):
        # 不弹原生列表, 改弹自绘 QMenu: 只显示另外两档, 按等级上色
        menu = QMenu(self)
        self._menu = menu
        menu.setCursor(Qt.PointingHandCursor)
        for lv, text in _LEVEL_OPTIONS:
            if lv == self._level:
                continue
            wa = QWidgetAction(menu)
            lab = _MenuLabel(text, _LEVEL_COLORS[lv], lv)
            lab.picked.connect(self._pick)
            wa.setDefaultWidget(lab)
            menu.addAction(wa)
        menu.popup(self.mapToGlobal(QPoint(0, self.height() + 2)))

    def _pick(self, lv):
        if self._menu is not None:
            self._menu.close()
            self._menu = None
        if lv != self._level:
            self._level = lv
            self._refresh()
            self.changed.emit(lv)


class _LevelsTable(QTableWidget):
    """支持「同档内」拖拽换位的表格: 拖拽只发信号, 由弹窗决定最终顺序(不可跨档)。"""

    reorderRequested = Signal(int, int)   # (src_row, dst_row)

    def __init__(self, rows: int, columns: int, parent=None):
        super().__init__(rows, columns, parent)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self._drag_row = -1

    def startDrag(self, actions):
        self._drag_row = self.currentRow()
        super().startDrag(actions)

    def dragEnterEvent(self, event):
        super().dragEnterEvent(event)
        event.acceptProposedAction()

    def dragMoveEvent(self, event):
        super().dragMoveEvent(event)
        event.acceptProposedAction()

    def dropEvent(self, event):
        src = self._drag_row
        self._drag_row = -1
        if src < 0:
            event.ignore()
            return
        row = self.indexAt(event.position().toPoint()).row()
        if row < 0:
            row = self.rowCount() - 1
        pos = self.dropIndicatorPosition()
        if pos in (QAbstractItemView.BelowItem, QAbstractItemView.OnItem):
            row += 1
        elif pos == QAbstractItemView.OnViewport:
            row = self.rowCount()
        event.acceptProposedAction()
        self.reorderRequested.emit(src, row)


class ParamLevelsDialog(QDialog):
    """⚙️ 参数分档设置: 每个参数选「必须一致 / 建议一致 / 仅供参考」, 按等级上色。

    改动立即生效并保存 —— 影响对比/属性/右侧参数对比/主界面分组标题的配色分档,
    **连「能否无损合并」的判定一起生效**(分组、全选可无损随之重算)。
    点「分档」表头切换排序方向; 同档内可拖拽换位(不可跨档); 顺序永久保存。
    """

    def __init__(self, on_change=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("参数分档设置")
        self.setMinimumSize(500, 640)
        self._on_change = on_change
        self._loading = False
        self._rows = [{"key": k, "cn": cn, "level": lv}
                      for k, cn, lv in grouping.FIELD_LEVELS]
        self._group_desc = bool(self._rows) and _group_of(self._rows[0]["level"]) == 2

        root = QVBoxLayout(self)
        root.setSpacing(10)

        tip = QLabel(
            "设为「必须一致」的参数不一致时，不能无损合并；把关键参数（像素格式、位深等）"
            "降级后强行合并，可能花屏或无声。设置影响全部范围：对比、属性、参数对比、"
            "主界面分组标题。")
        tip.setProperty("muted", True)
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.table = _LevelsTable(len(self._rows), 2)
        self.table.setHorizontalHeaderLabels(["参数", "分档"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        hdr = self.table.horizontalHeaderItem(1)
        if hdr is not None:
            hdr.setToolTip("点击切换排序：必须一致在前 / 仅供参考在前")
        self.table.reorderRequested.connect(self._reorder)
        self._combos: dict[str, _LevelCombo] = {}
        self._rebuild()
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        default_btn = QPushButton("设为默认")
        default_btn.setMinimumWidth(84)
        default_btn.clicked.connect(self._save_default)
        row.addWidget(default_btn)
        restore = QPushButton("恢复默认")
        restore.setMinimumWidth(84)
        restore.clicked.connect(self._restore_default)
        row.addWidget(restore)
        row.addStretch(1)
        done = QPushButton("完成")
        done.setMinimumWidth(84)
        done.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        done.setCursor(Qt.PointingHandCursor)
        done.setDefault(True)
        row.addWidget(done)
        root.addLayout(row)
        done.clicked.connect(self.accept)

        self.resize(520, 740)

    # ------------------------------------------------------------ 内部状态
    def _rebuild(self):
        self._loading = True
        self.table.setRowCount(0)
        self._combos = {}
        for r, row in enumerate(self._rows):
            self.table.insertRow(r)
            self.table.setRowHeight(r, 38)
            name = QTableWidgetItem(row["cn"])
            name.setToolTip(row["key"])
            self.table.setItem(r, 0, name)
            combo = _LevelCombo(row["level"])
            combo.changed.connect(lambda lv, k=row["key"]: self._on_level_changed(k, lv))
            self.table.setCellWidget(r, 1, combo)
            self._combos[row["key"]] = combo
        self._loading = False

    def _commit(self):
        if self._loading:
            return
        levels = {r["key"]: r["level"] for r in self._rows}
        order = [r["key"] for r in self._rows]
        grouping.apply_config(levels, order)
        merge_settings.save_field_levels(levels)
        merge_settings.save_field_order(order)
        if self._on_change:
            self._on_change()

    # ------------------------------------------------------------ 交互
    def _on_level_changed(self, key, lv):
        if self._loading:
            return
        for r in self._rows:
            if r["key"] == key:
                r["level"] = lv
                break
        self._rows = _sorted_by_group(self._rows, self._group_desc)
        self._rebuild()
        self._commit()

    def _on_header_clicked(self, col: int):
        if col != 1:
            return
        self._group_desc = not self._group_desc
        self._rows = _sorted_by_group(self._rows, self._group_desc)
        self._rebuild()
        self._commit()

    def _block_bounds(self, rows: list[dict], idx: int) -> tuple[int, int]:
        """rows 里 idx 所在分档组的连续区间 [lo, hi)。"""
        g = _group_of(rows[idx]["level"])
        lo = idx
        while lo > 0 and _group_of(rows[lo - 1]["level"]) == g:
            lo -= 1
        hi = idx
        while hi < len(rows) and _group_of(rows[hi]["level"]) == g:
            hi += 1
        return lo, hi

    def _reorder(self, src: int, dst: int):
        rows = self._rows
        if src < 0 or src >= len(rows):
            return
        lo, hi = self._block_bounds(rows, src)
        dst = max(lo, min(dst, hi))        # 不可跨档: 落点夹回同档区间
        if dst == src or dst == src + 1:
            return
        item = rows.pop(src)
        if dst > src:
            dst -= 1
        rows.insert(dst, item)
        self._rebuild()
        self._commit()

    def _save_default(self):
        self._commit()
        QMessageBox.information(self, "参数分档设置", "已把当前设置保存为默认。")

    def _restore_default(self):
        self._loading = True
        grouping.apply_config(None, None)
        merge_settings.save_field_levels(None)
        merge_settings.save_field_order(None)
        self._rows = [{"key": k, "cn": cn, "level": lv}
                      for k, cn, lv in grouping.DEFAULT_FIELD_LEVELS]
        self._group_desc = False
        self._rebuild()
        self._loading = False
        if self._on_change:
            self._on_change()


class _Thumb(QLabel):
    """可点击的预览图：左键点击后用系统默认程序打开文件（视频=播放器）。"""

    clicked = Signal(str)
    pressed = Signal(str)      # v193: 点预览图也要能选中这一列(自己盖住表格了)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("用默认程序打开")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.pressed.emit(self._path)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._path)
        super().mouseReleaseEvent(event)


class _LiveHeaderFilter(QObject):
    """v193: 表头拖动「实时换位」。

    Qt 自带的 `setSectionsMovable(True)` 只在**松手**那一刻才真正 moveSection ——
    拖的过程中只画一条插入指示线, 表头、预览图、参数值全都纹丝不动。
    这里把自带拖动关掉, 自己接管: 鼠标一越过相邻列就当场 `moveSection`,
    整列(表头文字 + 预览图 + 参数值)跟着走, 松手只是收尾。

    ⚠️ 第 0 列「参数」固定不动; 拖动结束要吞掉那一下 sectionClicked,
       否则会顺手把文件所在文件夹打开(原来的单击行为)。
    """

    def __init__(self, header: QHeaderView, owner):
        super().__init__(header)
        self._h = header
        self._owner = owner
        self._drag = None
        header.viewport().installEventFilter(self)

    def eventFilter(self, obj, ev):
        """表头随窗口销毁时, 过滤器可能还挂着最后一两次事件; 那时碰已删除的
        C++ 对象会抛 "Internal C++ object already deleted" —— 直接放行,
        别让关闭对比窗口在 app.log 里留一条「未捕获异常」。
        """
        try:
            return self._filter(obj, ev)
        except RuntimeError:
            return False

    def _filter(self, obj, ev):
        if obj is not self._h.viewport():
            return False
        t = ev.type()
        if t == QEvent.MouseButtonPress:
            self._drag = None
            self._owner._header_ctrl = bool(ev.modifiers() & Qt.ControlModifier)
            if ev.button() == Qt.LeftButton:
                x = ev.position().x()
                lg = self._h.logicalIndexAt(int(x))
                if lg > 0:                       # 第 0 列「参数」固定
                    sp = self._h.sectionViewportPosition(lg)
                    sz = self._h.sectionSize(lg)
                    near_edge = (x - sp < 5) or (sp + sz - x < 5)
                    if not near_edge:            # 贴近分隔线 = 让 Qt 调列宽
                        self._drag = {"visual": self._h.visualIndex(lg),
                                      "x0": x, "moved": False}
            return False
        if t == QEvent.MouseMove and self._drag is not None:
            if not (ev.buttons() & Qt.LeftButton):
                return False
            d = self._drag
            x = ev.position().x()
            if not d["moved"] and abs(x - d["x0"]) < 6:
                return False                     # 手抖不算拖, 让 Qt 照常处理
            d["moved"] = True
            lg_at = self._h.logicalIndexAt(int(x))
            if lg_at == 0:
                target = 1                       # 拖到「参数」列上 = 放到第一个文件位
            elif lg_at < 0:
                target = self._h.count() - 1     # 越过最右边 = 放到末尾
            else:
                target = self._h.visualIndex(lg_at)
            cur = d["visual"]
            if target != cur and target >= 1:
                self._h.moveSection(cur, target)
                d["visual"] = target
            return True                          # 吃掉了, 别让 Qt 再改 pressed section
        if t == QEvent.MouseButtonRelease:
            d = self._drag
            self._drag = None
            if d is not None and d["moved"]:
                self._owner._skip_header_click = True
            # 事件走完后统一清标志: sectionClicked 是同步 emit 的, 那时标志还在
            QTimer.singleShot(0, self._owner._clear_header_click_flags)
            return False
        if t == QEvent.ContextMenu:
            lg = self._h.logicalIndexAt(int(ev.pos().x()))
            self._owner._show_col_menu(ev.globalPos(), lg)
            return True
        return False


def _open_file(path: str):
    try:
        os.startfile(path)          # noqa: S606 (Windows)
    except Exception:
        pass


def _reveal(path: str):
    path = os.path.normpath(path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


_NAT_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _natural_key(text: str):
    """v198: 对比表按参数值排序用的自然序(数字段按数值比)。

    "H.265" > "H.264"、"1920×1080" > "1280×720"、"61.1 kbps" < "1.2 Mbps"
    这类值都能按人读的顺序排, 而不是按字符串逐字符比。
    """
    s = (text or "").strip().lower()
    parts, pos = [], 0
    for m in _NAT_NUM_RE.finditer(s):
        if m.start() > pos:
            parts.append((0, s[pos:m.start()]))
        try:
            parts.append((1, float(m.group())))
        except ValueError:
            parts.append((0, m.group()))
        pos = m.end()
    if pos < len(s):
        parts.append((0, s[pos:]))
    return tuple(parts)


class _CompareTable(QTableWidget):
    """v193: 列选择自己接管(整列为单位), 这样 Ctrl 加/减一列不会被 Qt 的
    ClearAndSelect 冲成只剩最后点的那一列。预览图是 cellWidget, 点它走
    `_Thumb.pressed` 信号, 两处最后都汇到 owner._select_col()。

    v198: 点第一列的参数名 = 按该行值给文件列排序; Ctrl+A = 只全选文件列。
    """

    def __init__(self, owner):
        super().__init__(1, 1)
        self._owner = owner

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        idx = self.indexAt(event.position().toPoint())
        col = idx.column() if idx.isValid() else self.columnAt(int(event.position().x()))
        # v198: 点在「参数」列的参数名上 → 按这一行的值给文件列排序
        if col == 0 and idx.isValid() and self._owner._is_param_row(idx.row()):
            self._owner._sort_by_param_row(idx.row())
            event.accept()
            return
        self._owner._select_col(col, bool(event.modifiers() & Qt.ControlModifier))
        # 不调 super: Qt 默认会 ClearAndSelect, 把 Ctrl 多选冲掉
        event.accept()

    def keyPressEvent(self, event):
        # v198: Qt 默认 Ctrl+A 会把第 0 列「参数」连表头一起选上, 而且窗口自己
        # 维护的 _sel_cols 不同步 → 右键菜单拿不到选中文件。这里只全选文件列。
        if event.key() == Qt.Key_A and (event.modifiers() & Qt.ControlModifier) \
                and not (event.modifiers() & Qt.ShiftModifier):
            self._owner._select_all_cols()
            event.accept()
            return
        super().keyPressEvent(event)


class _CompareInfoDelegate(QStyledItemDelegate):
    """v252: 在参数行画「表格线」+ 参数名右侧紧贴的灰色ⓘ角标。

    - 蓝线: 「建议一致/仅供参考」档位标题**上方那条表格线**变蓝加粗 —— 画在
      标题行前一参数行的单元格底边(标题行是 cellWidget, 不走 delegate)。
      ⚠️ 别画在标题 QLabel 的 border-top(v251 做错, 那是标题框的边不是表格线)。
    - 灰线: 视频/音频交界的**加粗灰表格线** —— 画在交界参数行的单元格顶边。
    - ⓘ: 自绘灰色上标角标, 悬浮显示常见格式说明。
    """
    ICON_W = 12          # ⓘ 角标直径(小号)
    GRAY_LINE_W = 2      # 视频/音频交界的加粗灰表格线(px)

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def _icon_rect(self, option, index) -> "QRect | None":
        key = index.data(Qt.UserRole) if index.isValid() else None
        if index.column() != 0 or not key or not grouping.field_desc(key):
            return None
        r = option.rect
        # 上标角标: 贴参数名右侧、靠行顶, 而不是垂直居中
        return QRect(r.right() - self.ICON_W - 4, r.top() + 1,
                     self.ICON_W, self.ICON_W)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        row = index.row()
        r = option.rect
        # 表格线: 直接画在单元格边界上(盖住 Qt 自带网格线), 不是标题框的边
        if row in self._owner._blue_bottom_rows:
            w = compare_model.BLUE_LINE_W
            painter.fillRect(r.left(), r.bottom() - w + 1, r.width(), w,
                             QColor(compare_model.SECTION_LINE_COLOR))
        if row in self._owner._gray_top_rows:
            w = self.GRAY_LINE_W
            painter.fillRect(r.left(), r.top(), r.width(), w,
                             QColor(compare_model.DIVIDER_COLOR))
        ir = self._icon_rect(option, index)
        if ir is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(compare_model.INFO_COLOR)); painter.setPen(Qt.NoPen)
        painter.drawEllipse(ir.adjusted(1, 1, -1, -1))
        painter.setPen(QColor("#FFFFFF"))
        f = QFont("Arial", 9); f.setBold(True); painter.setFont(f)
        painter.drawText(ir, Qt.AlignCenter, "i")
        painter.restore()

    def helpEvent(self, event, view, option, index):
        ir = self._icon_rect(option, index)
        if ir is not None and ir.contains(event.pos()):
            key = index.data(Qt.UserRole)
            QToolTip.showText(event.globalPos(), grouping.field_desc(key), view)
            return True
        return super().helpEvent(event, view, option, index)


class _NameEditDialog(QDialog):
    """v194: 单选重命名 —— 主名可编辑, 后缀作为只读标签贴在输入框右边。

    用户要的「输入框」+「.mp4」格式: 后缀一律取自磁盘文件的真实扩展名,
    既改不到也删不掉; 提交时原样拼回(只改显示名, 磁盘文件一个字节不动)。
    """

    def __init__(self, stem: str, ext: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("重命名")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        tip = QLabel("只改名字，后缀不可更改（磁盘文件名不变）。")
        tip.setProperty("muted", True)
        tip.setWordWrap(True)
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.setSpacing(0)
        self.edit = QLineEdit(stem or "")
        self.edit.setFixedHeight(28)
        self.edit.selectAll()
        row.addWidget(self.edit, 1)
        self.suffix = QLabel(ext or "")
        self.suffix.setFixedHeight(28)
        self.suffix.setAlignment(Qt.AlignCenter)
        self.suffix.setStyleSheet(
            f"QLabel{{color:{T.TEXT_SUB}; background:{T.SURFACE};"
            f" border:1px solid {T.BORDER}; border-left:none; padding:0px 8px;}}")
        row.addWidget(self.suffix, 0)
        lay.addLayout(row)

        btns = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.setMinimumWidth(84)
        ok = QPushButton("确定")
        ok.setMinimumWidth(84)
        ok.setDefault(True)
        ok.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        self.edit.returnPressed.connect(self.accept)
        self.setModal(True)

    def name(self) -> str:
        return self.edit.text()


class _BatchRenameDialog(QDialog):
    """v193: 对比窗口里批量改显示名（只改 Photo.name，磁盘文件一个字节都不动）。

    按选中列的点选顺序编号：前缀 + 自动补零的序号，输入框下方实时给前 3 个示例。
    """

    def __init__(self, count: int, prefix: str = "", parent=None,
                 exts: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("批量重命名")
        self.setMinimumWidth(420)
        self._count = max(1, int(count))
        # v194: 每个文件各自的后缀(取自磁盘路径), 生成的新名逐个拼回去
        self._exts = list(exts or [])
        if len(self._exts) < self._count:
            self._exts += [""] * (self._count - len(self._exts))
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        tip = QLabel(f"把选中的 {self._count} 个显示名改成「前缀 + 序号」。"
                     "各文件保留自己的后缀（不可更改），磁盘文件名不变。")
        tip.setProperty("muted", True)
        tip.setWordWrap(True)
        lay.addWidget(tip)

        form = QFormLayout()
        form.setSpacing(8)
        self.prefix = QLineEdit(prefix or "文件")
        self.prefix.selectAll()
        form.addRow("名称前缀", self.prefix)
        self.start = QSpinBox()
        self.start.setRange(0, 99999)
        self.start.setValue(1)
        form.addRow("起始序号", self.start)
        lay.addLayout(form)

        self.preview = QLabel("")
        self.preview.setProperty("muted", True)
        self.preview.setWordWrap(True)
        lay.addWidget(self.preview)
        self.prefix.textChanged.connect(self._refresh_preview)
        self.start.valueChanged.connect(self._refresh_preview)
        self._refresh_preview()

        row = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.setMinimumWidth(84)
        ok = QPushButton("确定")
        ok.setMinimumWidth(84)
        ok.setDefault(True)
        ok.setStyleSheet(
            f"background: {T.ACCENT}; border: 1px solid {T.ACCENT}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        self.setModal(True)

    def _refresh_preview(self, *_):
        shown = self.names()[:3]
        text = "示例：" + "、".join(shown)
        if self._count > 3:
            text += " …"
        self.preview.setText(text)

    def names(self) -> list[str]:
        base = self.prefix.text().strip() or "文件"
        st = int(self.start.value())
        end = st + self._count - 1
        width = max(2, len(str(end)))
        # v194: 序号后面拼各自的后缀(混合后缀时各保留各的, 参考示例按第一个)
        return [f"{base}{i:0{width}d}{self._exts[j] or ''}"
                for j, i in enumerate(range(st, end + 1))]


class CompareDialog(QDialog):
    """合并模式下选中多个文件时的逐参数对比（按严重程度排序）。

    布局：列头 = 「参数」(可点击切换排序方向) + 文件名(拖动实时换位 / 单击选列 /
    双击打开所在位置)，行 0 = 预览图(居中, 点击用默认程序打开)，之后按
    「必须一致 / 建议一致 / 仅参考」三档分段，每段一个跨列小标题。
    着色（参数名）：完全一致 = 绿；不一致且 fatal = 红、warn = 橙、仅参考 = 黑。

    v193: 整列可单选 / Ctrl 多选, 选中后右键「重命名」或「批量重命名」——
    只改显示名(Photo.name), 磁盘文件一个字节都不动。
    v198: 点第一列参数名按该行值给文件列排序(再点反向); Ctrl+A 只全选文件列;
    右键新增「N个移动到…」(真搬磁盘文件, 由主窗口执行并刷新所有界面)。
    """

    THUMB_W, THUMB_H = 132, 78
    GROUP_H = 26

    renamed = Signal(list)          # v193: 改了显示名 → 主窗口跟着刷新列表
    moveRequested = Signal(list)    # v198: 右键「N个移动到…」→ 主窗口搬文件

    def __init__(self, photos, specs: dict, parent=None, index=None):
        super().__init__(parent)
        # v192: 每个对比窗口带一个固定编号(标题上就能认出是哪一个)。
        # 剪辑右键的「发送到对比N」按这个号找窗口; 关掉某一个不重排号。
        self.cmp_index = index
        self.setWindowTitle(f"对比{index}" if index else "对比")
        self.setMinimumSize(360, 280)
        self._photos = list(photos)
        self._specs_list = [specs.get(p.path) for p in self._photos]
        self._desc = True                 # True=严重在上, False=不严重在上
        self._sel_cols: list[int] = []   # v193: 选中的文件列(logical index, 按点选顺序)
        self._skip_header_click = False
        self._header_ctrl = False
        self._sort_key: str | None = None  # v198: 当前按哪个参数行给文件列排序
        self._sort_desc = True             # v198: True=值大的在前(H.265 前 H.264 后)
        # v252: 表格线行号(delegate 在单元格边界画蓝/灰线), _fill_param_rows 重算
        self._blue_bottom_rows: set[int] = set()
        self._gray_top_rows: set[int] = set()
        self._build()
        self._autosize()

    def photo_paths(self) -> list[str]:
        """当前窗口里已有的文件(按 normcase 去重前的原路径)。"""
        return [p.path for p in self._photos]

    def add_photos(self, photos, specs: dict | None = None) -> int:
        """v192: 往已经开着的对比窗口里追加文件(新增列, 原地刷新)。

        单选「对比」先开一个只有 1 列的窗口, 之后「发送到对比N」把新选中的
        文件插进来 —— 窗口不重建、不闪、排序方向和已有列的位置全部保留。
        同一个文件重复发送直接忽略; 返回真正新增的列数。
        """
        have = {p.path for p in self._photos}
        new = [p for p in (photos or [])
               if p is not None and getattr(p, "path", "") not in have]
        if not new:
            return 0
        first = len(self._photos) + 1
        self._photos.extend(new)
        self._specs_list.extend([(specs or {}).get(p.path) for p in new])
        self.table.setColumnCount(len(self._photos) + 1)
        self._update_header_text()       # 补齐新列的表头
        for col in range(first, len(self._photos) + 1):
            self._put_thumb(col, self._photos[col - 1])
        self._fill_param_rows()          # 行数/档位/冲突着色按新列数重算
        self._apply_col_sort()           # v198: 追加的新列也归入当前排序
        self._autosize()
        return len(new)

    def _fill_param_rows(self):
        """重填参数行(排序方向切换时调用，列顺序/预览图不动)。

        行结构: [三档小标题(跨列)] + 参数行 … 小标题由 compare_model 的档位变化产生,
        与右侧「参数对比」共用同一套规则(参数清单/排序/上色都在 grouping + compare_model)。
        """
        rows = compare_model.build_rows(self._specs_list, compact=True,
                                        desc=self._desc)
        # 拆掉上一轮的小标题控件: 先 setCellWidget(None) 再重建,
        # 否则 deleteLater 之前旧控件还挂着, 换行数后会错位
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 0) is not None:
                self.table.setCellWidget(r, 0, None)
        self.table.clearSpans()

        n = len(self._photos)
        n_sec = sum(1 for i, r in enumerate(rows)
                    if i == 0 or rows[i - 1]["level"] != r["level"])
        # v252: 表格线行号(交给 delegate 在单元格边界画线)
        #   blue: 档位标题的上一参数行(底边画蓝线=标题上方那条表格线)
        #   gray: 视频/音频交界参数行(顶边画加粗灰线)
        self._blue_bottom_rows: set[int] = set()
        self._gray_top_rows: set[int] = set()
        self.table.setRowCount(len(rows) + n_sec + 1)

        r, prev = 1, object()
        for i, row in enumerate(rows):
            if row["level"] != prev:
                # 「建议一致/仅供参考」档位上方那条表格线变蓝(首档必须一致不加)
                if row["level"] in ("warn", None) and prev is not object() and r - 1 >= 1:
                    self._blue_bottom_rows.add(r - 1)
                self._add_section_row(r, row["level"], n + 1)   # 参数列 + 每文件一列
                prev = row["level"]
                r += 1
            # 视频/音频交界: 这一参数行的顶边画加粗灰表格线
            if compare_model.media_divider_before(rows, i):
                self._gray_top_rows.add(r)
            arrow = ""
            if self._sort_key == row["key"]:
                arrow = " ↓" if self._sort_desc else " ↑"
            name_item = QTableWidgetItem(row["cn"] + arrow)
            name_item.setData(Qt.UserRole, row["key"])
            if arrow:
                name_item.setToolTip("按这一行的值给文件列排序；再点一次反向")
            elif row["key"] == "bitrate_mode":
                name_item.setToolTip("按前 20 秒逐秒码率的波动估计；文件里通常没有写死 CBR/VBR 标记。")
            name_item.setForeground(QBrush(QColor(row["color"])))
            name_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 0, name_item)
            for i, v in enumerate(row["values"], 1):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, i, item)
            r += 1
        # 行重建后把整列选中态画回去（QTableWidgetItem 换过一遍，不然高亮会丢）
        self._apply_col_selection()

    def _add_section_row(self, row: int, level, ncols: int):
        """跨列小标题: 必须一致 / 建议一致 / 仅参考。"""
        lab = QLabel(compare_model.section_title(level))
        color = (T.DANGER if level == "fatal"
                 else T.WARN if level == "warn" else T.TEXT_SUB)
        lab.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 600;"
            f" padding-left: 8px; background: {T.SURFACE_ALT}; border-radius: 4px;")
        lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setSpan(row, 0, 1, ncols)
        self.table.setCellWidget(row, 0, lab)
        self.table.setRowHeight(row, self.GROUP_H)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        n = len(self._photos)
        self.table = _CompareTable(self)
        self.table.setItemDelegate(_CompareInfoDelegate(self))  # v251: 参数名右侧ⓘ+悬浮说明
        self.table.setColumnCount(n + 1)
        self.table.setRowCount(1)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        # v193: 整列可单选 / Ctrl 多选(选中逻辑自己接管, 见 _CompareTable)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectColumns)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_menu)
        header = self.table.horizontalHeader()
        # 自带 movable 只在松手那一刻换位, 拖的过程中什么都不动 —— 关掉,
        # 交给 _LiveHeaderFilter 在 mouseMove 里实时 moveSection。
        header.setSectionsMovable(False)
        header.setFirstSectionMovable(False)  # 「参数」列固定
        header.setSectionsClickable(True)     # 点「参数」切排序 / 点文件名选列
        header.setMinimumSectionSize(110)
        header.setSectionResizeMode(QHeaderView.Interactive)   # 列宽由 _autosize 手动算
        header.sectionClicked.connect(self._on_header_clicked)
        header.sectionDoubleClicked.connect(self._on_header_double_clicked)
        self._hdr_filter = _LiveHeaderFilter(header, self)

        # 列头: 「参数」+ 各文件名
        self._update_header_text()

        # 行 0: 预览图(居中, 点击播放)
        self.table.setRowHeight(0, self.THUMB_H + 8)
        for i, p in enumerate(self._photos, 1):
            self._put_thumb(i, p)

        self._fill_param_rows()
        root.addWidget(self.table, 1)

        # 规格可能是后到的（「添加」弹窗里先开窗、再后台 ffprobe）：这行给个进度口径，
        # 免得先开出来的空表看着像坏掉了。set_specs() 会把它填掉或改文字。
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        self._status.setVisible(False)
        root.addWidget(self._status)

        hint = QLabel("左右拖动文件名表头可实时换位；点参数名按这一行的值排文件列（再点反向）；"
                      "点单元格或文件名选列（Ctrl 连选、Ctrl+A 全选文件列），"
                      "选中后右键「N个移动到…」/ 重命名 / 打开所在位置 / 用默认程序打开；"
                      "双击文件名打开所在位置，点预览图播放；点「参数」切换严重程度排序方向。")

        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        root.addWidget(hint)

    def _put_thumb(self, col: int, p):
        """给第 col 列放一张可点击打开的预览图(建窗口和追加列共用)。"""
        thumb = _Thumb(p.path)
        pix = getattr(p, "thumb", None)
        if pix is not None and not pix.isNull():
            thumb.setPixmap(pix.scaled(self.THUMB_W, self.THUMB_H,
                                       Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            thumb.setText("无预览")
            thumb.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 11px;")
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setFixedSize(self.THUMB_W, self.THUMB_H)
        thumb.clicked.connect(_open_file)
        # v193: 预览图盖住了表格, 点它也要能选中这一列(和点参数格同一套口径)
        thumb.pressed.connect(
            lambda _p, c=col: self._select_col(
                c, bool(QApplication.keyboardModifiers() & Qt.ControlModifier)))
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addStretch(1)
        hl.addWidget(thumb, 0, Qt.AlignCenter)
        hl.addStretch(1)
        self.table.setCellWidget(0, col, holder)

    def show_loading(self, text: str = "正在读取视频参数…"):
        """先把窗口开出来，参数随后补（「添加」弹窗里的对比走这条路）。"""
        self._status.setText(text)
        self._status.setVisible(True)

    def set_specs(self, specs: dict, hint: str = ""):
        """规格到齐后原地重填参数行：列头 / 预览图 / 栏位顺序都不动。"""
        self._specs_list = [(specs or {}).get(p.path) for p in self._photos]
        self._fill_param_rows()
        self._status.setText(hint)
        self._status.setVisible(bool(hint))
        self._autosize()

    def _autosize(self):
        """窗口随内容自动缩放：列宽按「预览图 / 文件名 / 值」最宽者，行高按内容。"""
        n = len(self._photos)
        header = self.table.horizontalHeader()
        fm = QFontMetrics(self.table.font())

        for i in range(n + 1):
            w = self.table.sizeHintForColumn(i) + 16     # item 渲染余量
            if i == 0:
                w = max(w, fm.horizontalAdvance("音频采样格式") + 28)
            else:
                p = self._photos[i - 1]
                w = max(w, self.THUMB_W + 8, fm.horizontalAdvance(p.name) + 24)
            header.resizeSection(i, w)

        total_w = header.length() + 8
        total_h = (self.table.verticalHeader().length()
                   + header.height() + 8)
        screen = QApplication.primaryScreen().availableGeometry()
        w = max(self.minimumWidth(), min(total_w + 40, int(screen.width() * 0.92)))
        h = max(300, min(total_h + 64, int(screen.height() * 0.9)))
        self.resize(w, h)

    def _update_header_text(self):
        head = QTableWidgetItem("参数 ↓" if self._desc else "参数 ↑")
        head.setToolTip("点击切换排序方向（严重在上 / 不严重在上）")
        self.table.setHorizontalHeaderItem(0, head)
        for i, p in enumerate(self._photos, 1):
            it = QTableWidgetItem(p.name)
            it.setToolTip(f"{p.path}\n（左右拖动实时换位，单击选列、Ctrl 连选，双击打开所在位置）")
            it.setTextAlignment(Qt.AlignCenter)
            self.table.setHorizontalHeaderItem(i, it)

    def refresh_names(self):
        """别的对比窗口改了同一个文件的显示名时，把表头文字同步过来。"""
        for i, p in enumerate(self._photos, 1):
            it = self.table.horizontalHeaderItem(i)
            if it is not None:
                it.setText(p.name)
        self._autosize()

    def refresh_after_move(self):
        """v198: 文件被搬走后, 表头名字与预览图路径同步换新。

        _photos 与主列表是同一批 Photo 对象(主窗口已原地改 path/name);
        预览图 cellWidget 里存的却是构造时的旧路径, 不重建会点不开文件。
        """
        self._update_header_text()
        for i, p in enumerate(self._photos, 1):
            old = self.table.cellWidget(0, i)
            if old is not None:
                self.table.removeCellWidget(0, i)
                old.deleteLater()
            self._put_thumb(i, p)
        self._apply_col_selection()
        self._autosize()

    # ------------------------------------------------- v198 按参数排文件列 / 全选
    def _is_param_row(self, row: int) -> bool:
        """第 0 列里带参数 key 的才是参数行(行 0 预览、三档小标题都不算)。"""
        if row <= 0 or row >= self.table.rowCount():
            return False
        item = self.table.item(row, 0)
        return item is not None and bool(item.data(Qt.UserRole))

    def _row_of_key(self, key: str) -> int | None:
        for r in range(1, self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is not None and it.data(Qt.UserRole) == key:
                return r
        return None

    def _sort_by_param_row(self, row: int):
        """点参数名: 按这一行各文件的值重排文件列; 再点同一行反向。"""
        item = self.table.item(row, 0)
        key = item.data(Qt.UserRole) if item is not None else None
        if not key:
            return
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = True       # 首次: 值大的在前(H.265 前 H.264 后)
        self._apply_col_sort()
        self._fill_param_rows()          # 参数名上的箭头跟着刷新
        self._autosize()

    def _apply_col_sort(self):
        """只改表头的 visual 顺序, 数据仍按 logical index 对应文件。

        ⚠️ 绝不能去重排 _photos / _specs_list: 预览图、参数值、选中列都按
        logical index 存放, Qt 会让它们一起跟着 visual 位置走 —— 与「拖动表头
        实时换位」是同一套机制, 两者天然兼容。
        """
        if not self._sort_key:
            return
        n = len(self._photos)
        if n < 2:
            return
        row = self._row_of_key(self._sort_key)
        if row is None:
            return
        vals = {}
        for c in range(1, n + 1):
            it = self.table.item(row, c)
            vals[c] = (it.text() if it is not None else "").strip()
        have = [c for c in range(1, n + 1) if vals[c] and vals[c] != "—"]
        missing = [c for c in range(1, n + 1) if c not in have]
        have.sort(key=lambda c: _natural_key(vals[c]), reverse=self._sort_desc)
        order = have + missing           # 空值恒排末尾, 不受升降序影响
        hdr = self.table.horizontalHeader()
        for i, logical in enumerate(order, 1):
            cur = hdr.visualIndex(logical)
            if cur >= 1 and cur != i:
                hdr.moveSection(cur, i)

    def _select_all_cols(self):
        """v198: Ctrl+A 只选全部文件列, 不碰第 0 列「参数」及其表头。"""
        self._sel_cols = list(range(1, len(self._photos) + 1))
        self._apply_col_selection()

    # ------------------------------------------------------- v193 选列 / 重命名
    def _clear_header_click_flags(self):
        """一次表头点击/拖动结束后统一复位（sectionClicked 是同步 emit 的）。"""
        self._skip_header_click = False
        self._header_ctrl = False

    def _select_col(self, col: int, ctrl: bool = False):
        """选中一整列（logical index）。Ctrl = 加/减一列，不选「参数」列。"""
        if col is None or col < 1 or col > len(self._photos):
            if not ctrl:
                self._sel_cols = []
        elif ctrl:
            if col in self._sel_cols:
                self._sel_cols.remove(col)
            else:
                self._sel_cols.append(col)
        else:
            self._sel_cols = [col]
        self._apply_col_selection()

    def _apply_col_selection(self):
        """把 _sel_cols 画成整列高亮：表头 + 参数值 + 预览图一起亮。"""
        sm = self.table.selectionModel()
        if sm is None:
            return
        sm.clearSelection()
        if self.table.rowCount() < 1:
            return
        model = self.table.model()
        for c in self._sel_cols:
            if 1 <= c <= len(self._photos):
                sm.select(model.index(0, c),
                          QItemSelectionModel.Select | QItemSelectionModel.Columns)

    def _selected_photos(self) -> list[Photo]:
        return [self._photos[c - 1] for c in self._sel_cols
                if 1 <= c <= len(self._photos)]

    def _on_header_clicked(self, logical_index: int):
        # 拖完表头松手那一下 Qt 仍会补一个 sectionClicked，别把它当成选列 / 切排序。
        if self._skip_header_click:
            return
        if logical_index == 0:
            self._desc = not self._desc
            self._update_header_text()
            self._fill_param_rows()
        elif 1 <= logical_index <= len(self._photos):
            self._select_col(logical_index, self._header_ctrl)

    def _on_header_double_clicked(self, logical_index: int):
        if 1 <= logical_index <= len(self._photos):
            _reveal(self._photos[logical_index - 1].path)

    def _on_table_menu(self, pos):
        """表格本体右键：定位到那一列，再弹选列菜单。"""
        col = self.table.columnAt(int(pos.x()))
        self._show_col_menu(self.table.viewport().mapToGlobal(pos), col)

    def _show_col_menu(self, global_pos, col=None):
        """表头 / 表格右键统一入口：改名 + 打开所在位置 / 默认程序打开。"""
        if col is not None and 1 <= col <= len(self._photos) \
                and col not in self._sel_cols:
            self._select_col(col, False)          # 右键没选中的列 = 先只选它
        photos = self._selected_photos()
        if not photos:
            return
        menu = QMenu(self)
        menu.setCursor(Qt.PointingHandCursor)
        # v198: 全选/多选后可以直接搬文件(主界面「N个移动到…」同一个弹窗)
        menu.addAction(f"{len(photos)}个移动到…", self._move_selected)
        if len(photos) == 1:
            menu.addAction("重命名…", self._rename_one)
        else:
            menu.addAction("批量重命名…", self._rename_many)
        menu.addSeparator()
        menu.addAction("打开文件所在位置", self._reveal_selected)
        menu.addAction("用默认程序打开", self._open_selected)
        menu.exec(global_pos)

    def _move_selected(self):
        """v198: 把选中的文件交给主窗口搬走(主列表 / 规格缓存 / 所有对比窗口同步刷新)。"""
        photos = self._selected_photos()
        if not photos:
            return
        try:
            # 传 Photo 对象本身: 搬完主窗口会原地改 path/name, 对比窗口随后刷新
            self.moveRequested.emit(list(photos))
        except RuntimeError:
            pass

    def _reveal_selected(self):
        """v194: 选中集逐个在资源管理器里定位(和表头双击同一个函数)。"""
        for p in self._selected_photos():
            _reveal(p.path)

    def _open_selected(self):
        """v194: 选中集逐个用系统默认程序打开(和预览图左键同一个函数)。"""
        for p in self._selected_photos():
            _open_file(p.path)

    def _rename_one(self):
        photos = self._selected_photos()
        if len(photos) != 1:
            return
        p = photos[0]
        # v194: 后缀拆出来只作只读标签, 输入框里只有主名
        stem, ext = split_name_stem(p.name, p.path)
        dlg = _NameEditDialog(stem, ext, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new = (dlg.name() or "").strip()
        if not new or new == stem:
            return
        p.name = new + ext
        self._after_rename([p.path])

    def _rename_many(self):
        photos = self._selected_photos()
        if len(photos) < 2:
            return
        exts = [split_name_stem(p.name, p.path)[1] for p in photos]
        dlg = _BatchRenameDialog(len(photos), parent=self, exts=exts)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        names = dlg.names()
        paths = []
        for p, name in zip(photos, names):
            if not name or name == p.name:
                continue
            p.name = name
            paths.append(p.path)
        if paths:
            self._after_rename(paths)

    def _after_rename(self, paths: list[str]):
        """表头原地刷新 + 通知主窗口把列表里同一批文件的名字也刷掉。"""
        self._update_header_text()
        self._apply_col_selection()
        self._autosize()
        try:
            self.renamed.emit(list(paths))
        except RuntimeError:
            pass


def confirm_delete_files(parent, prompt_html: str, names: list, paths: list,
                         *, center: bool = True) -> bool:
    """永久删除确认框: 居中提示 + 文件清单 + 文件名悬浮显示略缩图。

    - 提示文字(支持富文本, "永久删除"可标红)。
    - 文件清单逐条列出; 鼠标停在某个文件名上即弹出该文件的略缩图。
    - 底部「否」居左、「是」居右(红色), 无英文 Y/N、无问号图标。
    """
    dlg = _DeleteConfirmDialog(prompt_html, names, paths, parent)
    return dlg.exec() == QDialog.DialogCode.Accepted


class _DeleteConfirmDialog(QDialog):
    def __init__(self, prompt_html, names, paths, parent=None):
        super().__init__(parent)
        self.setWindowTitle("永久删除文件")
        self.setMinimumWidth(460)
        self.setMinimumHeight(260)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        msg = QLabel(prompt_html)
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.RichText)
        msg.setAlignment(Qt.AlignCenter)
        lay.addWidget(msg)

        self._paths = list(paths)
        self.list = QListWidget()
        self.list.addItems([str(n) for n in names])
        self.list.setMouseTracking(True)
        self.list.viewport().setMouseTracking(True)
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setFrameShape(QListWidget.NoFrame)
        self.list.setStyleSheet(
            f"QListWidget::item {{ padding: 3px 8px; border-radius: 5px; }}"
            f"QListWidget::item:hover {{ background: {T.ACCENT_SOFT}; }}")
        self.list.itemEntered.connect(self._on_item_entered)
        self.list.viewport().installEventFilter(self)
        lay.addWidget(self.list, 1)

        # 悬浮略缩图窗口(无边框小浮窗, 不抢焦点、不吃鼠标)
        self._thumb = QLabel(
            None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._thumb.setAttribute(Qt.WA_ShowWithoutActivating)
        self._thumb.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._thumb.setStyleSheet(
            "border: 1px solid #C8CDD6; background: #FFFFFF;")
        self._thumb.setAlignment(Qt.AlignCenter)
        self._thumb.setScaledContents(False)
        self._thumb.resize(THUMB_PREVIEW + 2, THUMB_PREVIEW + 2)
        self._thumb.hide()
        self._thumb_row = -1

        row = QHBoxLayout()
        no_btn = QPushButton("否")
        no_btn.setMinimumWidth(84)
        no_btn.setDefault(True)
        yes_btn = QPushButton("是")
        yes_btn.setMinimumWidth(84)
        yes_btn.setStyleSheet(
            f"background: {T.DANGER}; border: 1px solid {T.DANGER}; color: #FFFFFF;"
            f" font-weight: 600; border-radius: 7px; padding: 6px 18px;")
        yes_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(no_btn)
        row.addStretch(1)
        row.addWidget(yes_btn)
        lay.addLayout(row)

        no_btn.clicked.connect(self.reject)
        yes_btn.clicked.connect(self.accept)

    def _thumb_for(self, path: str):
        """取缩略图: 缓存 → QPixmap 直读 → Pillow/ffmpeg 解码(兼容 HEIC/视频)。"""
        try:
            from ..core.thumbs import thumb_pixmap
            return thumb_pixmap(path)
        except Exception:
            pm = QPixmap(path)
            return pm if not pm.isNull() else QPixmap()

    def _on_item_entered(self, item):
        self._show_thumb(self.list.row(item))

    def _show_thumb(self, row_idx: int):
        if row_idx == self._thumb_row and self._thumb.isVisible():
            return
        self._thumb_row = row_idx
        if not (0 <= row_idx < len(self._paths)):
            self._thumb.hide()
            return
        pm = self._thumb_for(self._paths[row_idx])
        if pm.isNull():
            self._thumb.hide()
            return
        pm = pm.scaled(THUMB_PREVIEW, THUMB_PREVIEW,
                       Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._thumb.setPixmap(pm)
        self._thumb.resize(pm.width() + 2, pm.height() + 2)   # +2: 1px 边框
        item = self.list.item(row_idx)
        if item is None:
            self._thumb.hide()
            return
        r = self.list.visualItemRect(item)
        anchor = self.list.viewport().mapToGlobal(r.topRight())
        screen = self.list.screen()
        x, y = anchor.x() + 10, anchor.y()
        if screen is not None:
            avail = screen.availableGeometry()
            if x + self._thumb.width() > avail.right():
                x = anchor.x() - self._thumb.width() - 10
                if x < avail.left():
                    x = max(avail.left(), avail.right() - self._thumb.width())
            if y + self._thumb.height() > avail.bottom():
                y = max(avail.top(), avail.bottom() - self._thumb.height())
        self._thumb.move(x, y)
        self._thumb.show()
        self._thumb.raise_()

    def _hide_thumb(self):
        self._thumb_row = -1
        self._thumb.hide()

    def eventFilter(self, obj, ev):
        if obj is self.list.viewport():
            et = ev.type()
            if et == QEvent.Type.MouseMove:
                # 不依赖 itemEntered(某些环境下不触发), 用鼠标移动 + itemAt 兜底
                pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
                idx = self.list.indexAt(pos)
                self._show_thumb(idx.row() if idx.isValid() else -1)
            elif et in (QEvent.Type.Leave, QEvent.Type.Hide):
                self._hide_thumb()
        return super().eventFilter(obj, ev)


class _InfoLabel(QLabel):
    """小圆形 ⓘ 提示图标, 鼠标一进入就**立即**显示 tooltip(不等默认 ~1 秒延迟)。

    用普通 QLabel + setToolTip 时, 12~14px 的小图标命中区很小, 鼠标稍微移动
    tooltip 就取消, 加上默认延迟, 实际几乎看不到提示。这里重写 enter/leave
    用一个无边框的 QLabel 做弹出窗口(字体大小完全可控, 不受全局 QToolTip 影响),
    进入立即显示、移出立即消失。

    v48 改用 QLabel 弹窗: 字号 20px, 颜色用主题里的 TOOLTIP_BG/TOOLTIP_FG。
    v49 字号调为 17px(20 偏大, 17 视觉更柔和), 弹窗最大宽 360, 内容排版用
    "  1. /  2. /  3.  + 缩进说明" 的结构而不是一长段中文。
    """

    def __init__(self, tip: str = "", size: int = 14, parent=None):
        super().__init__("i", parent)
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._tip = tip
        self._popup = None
        self.setStyleSheet(
            f"background: {T.ACCENT}; color: #FFFFFF; border-radius: {size // 2}px; "
            f"font-size: 9px; font-weight: bold; margin: 0;")

    def _ensure_popup(self) -> QLabel:
        """惰性创建 popup: 无边框 QLabel, 字号 17, 套主题的 tooltip 配色。"""
        if self._popup is None:
            p = QLabel(self._tip, None)
            p.setWindowFlags(
                Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            p.setAttribute(Qt.WA_ShowWithoutActivating)
            p.setWordWrap(True)
            p.setTextFormat(Qt.PlainText)
            p.setStyleSheet(
                f"background: {T.TOOLTIP_BG}; color: {T.TOOLTIP_FG};"
                f" padding: 8px 12px; border-radius: 6px;"
                f" font-size: 17px; line-height: 1.45;")
            # 限制弹窗最大宽度, 让长说明按 wordWrap 折成多行(否则一行拉很长)
            p.setMaximumWidth(360)
            # 用 popup 自身字体设置会被 QLabel 内字体样式表覆盖,
            # 但显式 setFont 还能影响换行计算, 一起设
            f = p.font()
            f.setPointSize(17)
            p.setFont(f)
            self._popup = p
        return self._popup

    def enterEvent(self, ev):
        pos = (ev.globalPosition().toPoint()
               if hasattr(ev, "globalPosition") else ev.globalPos())
        p = self._ensure_popup()
        # 让 popup 自适应 tip 文本宽高(再加点边距)
        p.adjustSize()
        p.move(pos.x() + 10, pos.y() + 12)
        p.show()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self._popup is not None:
            self._popup.hide()
        super().leaveEvent(ev)

    def hideEvent(self, ev):
        # 父窗口隐藏/关闭时同步关闭弹窗, 避免残留
        if self._popup is not None:
            self._popup.hide()
        super().hideEvent(ev)


class _SegSpinBox(QSpinBox):
    """带前导零补位的 QSpinBox: ``08`` / ``31`` 显示两位小数(非补位用年那种 4 位)。"""

    def __init__(self, pad: int = 2, parent=None):
        super().__init__(parent)
        self._pad = pad

    def textFromValue(self, value: int) -> str:
        return str(value).zfill(self._pad)

    def valueFromText(self, text: str) -> int:
        t = text.strip()
        try:
            return int(t)
        except (TypeError, ValueError):
            return self.minimum()


class _DateTimeSegments(QWidget):
    """日期时间输入: 年 / 月 / 日 / 时 / 分 / 秒 六个独立元素。

    与 ``QDateTimeEdit`` 不同, 每个段(年月日时分秒)都是一个独立的 ``QSpinBox``,
    互不粘连:

    - **第一次点击只显示蓝选**(selection 蓝底白字), 没有竖线光标
    - **再次点击同一段才显示闪烁竖线光标**, 可精细编辑单个数字
    - 上下键增减, 左右键跨段, Home/End 跳到首/末段
    - 日的范围随 月 / 年 联动 (28-31), 写入时跨月自动回卷
    - 没有内置上下小三角按钮, 减少视觉噪音
    """

    changed = Signal()

    def __init__(self, dt: datetime, parent=None):
        super().__init__(parent)
        self._dt = dt

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)            # 紧凑: 6 段与分隔符完全挨紧

        self._spins: list[QSpinBox] = []
        seps = ["-", "-", " ", ":", ":"]
        specs = [
            (2020, 2099, dt.year, 4, "d"),   # 年 4 位
            (1,    12,   dt.month, 2, "d"),  # 月 2 位
            (1,    31,   dt.day,   2, "d"),  # 日 2 位
            (0,    23,   dt.hour,  2, "d"),  # 时
            (0,    59,   dt.minute, 2, "d"),  # 分
            (0,    59,   dt.second, 2, "d"),  # 秒
        ]
        for idx, (lo, hi, val, w, _fmt) in enumerate(specs):
            sp = _SegSpinBox(pad=w)
            sp.setRange(lo, hi)
            sp.setValue(val)
            # 紧凑尺寸: 年段 4 位 / 其余 2 位, 只装数字。
            # 宽度按字体实际像素算(数字 0 的宽度 × 位数 + 余量), 不能用硬编码 28/18 —
            # 之前年段 28px 装不下 4 位数字(fm("0000")=48px), "2026" 的最后一位 6 被截掉。
            _fm = QFontMetrics(sp.font())
            sp.setFixedWidth(_fm.horizontalAdvance("0" * w) + 4)
            sp.setAlignment(Qt.AlignCenter)
            sp.setStyleSheet(_DT_SEGMENT_QSS)
            sp.setButtonSymbols(QAbstractSpinBox.NoButtons)
            sp.setFocusPolicy(Qt.StrongFocus)
            _hide_spinbox_cursor(sp)       # 起始时无光标 (蓝色 selection 模式)
            # 悬浮在段数字上时显示箭头光标, 而不是 I 型文本光标
            sp.setCursor(Qt.ArrowCursor)
            le = sp.findChild(QLineEdit)
            if le is not None:
                le.setCursor(Qt.ArrowCursor)
            sp.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
            outer.addWidget(sp)
            self._spins.append(sp)
            # 数字后的分隔符(最后一段后不再加)
            if idx < len(seps):
                sep = seps[idx]
                lbl = QLabel(sep)
                lbl.setFixedWidth(6)
                lbl.setStyleSheet(
                    f"color: {T.TEXT_WEAK}; font-weight: 600; "
                    f"margin: 0 -2px 0 -2px;")
                lbl.setAlignment(Qt.AlignCenter)
                outer.addWidget(lbl)

        # 默认焦点落在第一个段(年), 触发 selectAll + 隐藏 caret (蓝选模式)
        self._spins[0].setFocus()

        # 信号: 任意段变化都通知外部刷新预览; 月/年变化时联动日的范围
        for i, sp in enumerate(self._spins):
            sp.valueChanged.connect(self._on_value_changed)
        self._refine_day_range()

        # 事件过滤: 双击显示 caret (精细化输入), 失焦或单击恢复蓝选模式
        for i, sp in enumerate(self._spins):
            sp.installEventFilter(self)
            sp.setProperty("seg_index", i)

    # --- 联动: 日的可允许范围随 月/年 变化 ---
    def _refine_day_range(self):
        try:
            year = self._spins[0].value()
            month = self._spins[1].value()
        except Exception:
            return
        last_day = _last_day_of_month(year, month)
        day = self._spins[2]
        day.blockSignals(True)
        day.setRange(1, last_day)
        # 如果当前值超出, 自动回卷
        if day.value() > last_day:
            day.setValue(last_day)
        day.blockSignals(False)

    def _on_value_changed(self, _val: int):
        # 月或年变化时先调一次联动
        self._refine_day_range()
        self.changed.emit()

    # --- 左右键跨段, Home/End 跳首末, 第一次点蓝选/第二次点显示 caret ---
    def eventFilter(self, obj, event):
        # 仅处理 6 段 spinbox 的事件
        if obj not in self._spins:
            return super().eventFilter(obj, event)
        et = event.type()
        if et == QEvent.Type.FocusIn:
            # 进入焦点: 整段蓝选, 隐藏 caret (默认蓝选模式)
            self._set_caret_width(obj, 0)
            QTimer.singleShot(0, lambda o=obj: o.selectAll())
        elif et == QEvent.Type.FocusOut:
            # 离开焦点: 强制恢复蓝选模式
            self._set_caret_width(obj, 0)
        elif et == QEvent.Type.MouseButtonDblClick and event.button() == Qt.LeftButton:
            # 双击: 显示 caret 进入精细化输入模式 (再点会落在点击位置)
            self._set_caret_width(obj, 1)
            return False  # 让 Qt 也处理这次点击, 让 caret 落在点击位置
        if et == QEvent.Type.KeyPress:
            k = event.key()
            i = obj.property("seg_index")
            if k == Qt.Key_Right:
                self._focus_seg(min(i + 1, len(self._spins) - 1))
                return True
            if k == Qt.Key_Left:
                self._focus_seg(max(i - 1, 0))
                return True
            if k == Qt.Key_Home:
                self._focus_seg(0)
                return True
            if k == Qt.Key_End:
                self._focus_seg(len(self._spins) - 1)
                return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _set_caret_width(spin: QSpinBox, width: int):
        le = spin.findChild(QLineEdit)
        if le is not None:
            le.setProperty("cursorWidth", width)

    def _focus_seg(self, idx: int):
        sp = self._spins[idx]
        sp.setFocus()
        # arm_caret 状态由 FocusIn 事件统一处理, 这里不直接 selectAll
        sp.setProperty("arm_caret", True)
        self._set_caret_width(sp, 0)

    # --- 暴露给外部 ---
    def datetime(self) -> datetime:
        y, mo, d = self._spins[0].value(), self._spins[1].value(), self._spins[2].value()
        h, mi, s = self._spins[3].value(), self._spins[4].value(), self._spins[5].value()
        return datetime(y, mo, d, h, mi, s)


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    first_next = datetime(next_year, next_month, 1)
    last = first_next - timedelta(days=1)
    return last.day


class _BaseDialog(QDialog):
    def __init__(self, parent=None, title: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)


class _IntervalSpinBox(QDoubleSpinBox):
    """间隔数值输入: 上下键增减, 左右键在整数/小数部分间切换。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.setDecimals(2)
        # v217: 下限从 0.01 提到 1 秒。时间戳落盘只到秒(EXIF/XMP/QuickTime 都
        # 不写亚秒), 间隔 <1 秒时多张会被写成同一时间, 读回校验也发现不了。
        # 与其"填了不生效", 不如直接不让填。
        self.setRange(1.0, 999999)
        self.setSingleStep(1.0)
        self.setValue(1.0)
        self._whole = True

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Left:
            self._whole = True
            self.selectAll()
            event.accept()
            return
        if key == Qt.Key_Right:
            self._whole = False
            text = self.text()
            dot = text.find(".")
            if dot >= 0:
                self.setSelection(dot + 1, len(text) - dot - 1)
            event.accept()
            return
        if key in (Qt.Key_Up, Qt.Key_Down):
            step = 1.0 if self._whole else 0.01
            if key == Qt.Key_Down:
                step = -step
            self.setValue(self.value() + step)
            event.accept()
            return
        super().keyPressEvent(event)


class IntervalDialog(_BaseDialog):
    """自动间隔排序。"""

    def __init__(self, photos: list[Photo], parent=None):
        super().__init__(parent, "自动间隔排序")
        self.photos = photos
        self._build()
        self._update_preview()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        info = QLabel(f"将对 <b>{len(self.photos)}</b> 张照片按当前列表顺序重新分配时间")
        lay.addWidget(info)

        # ---- 第一行: 起始时间 + 每张间隔 + 方向 ----
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        lbl_start = QLabel("起始时间")
        # 与第二行"三个时间"标签同样留 6px 余量(避免汉字右边缘被切), 两行左边缘对齐
        lbl_start.setFixedWidth(lbl_start.sizeHint().width() + 6)
        row1.addWidget(lbl_start)

        # 起始时间: 6 个独立元素(年月日时分秒), 无闪烁光标, 选中整段变蓝。
        self.start_segments = _DateTimeSegments(timeplan.suggest_start(self.photos))
        self.start_segments.changed.connect(lambda _=0: self._update_preview())
        row1.addWidget(self.start_segments, 2)

        lbl_iv = QLabel("间隔")
        lbl_iv.setFixedWidth(30)
        row1.addWidget(lbl_iv)

        self.value_spin = _IntervalSpinBox()
        self.value_spin.setFixedWidth(70)
        # 注意: 数值框里不再带单位后缀, 单位在右边的下拉中体现
        row1.addWidget(self.value_spin)

        # 间隔单位下拉。
        # ★ v47 关键修复: 全局 QSS 里 QComboBox { min-width: 80px; padding: 6px 10px; }
        #   把 sizeHint 抬到 ~204px, 之前 max(sizeHint*0.5, 内容宽) 在真机算出 102px
        #   并被 setFixedWidth 写死 —— 比 v44 的 56px 还宽, 这就是用户反复说
        #   "改了没变化"的真正原因(离线无主题测试量出 32px 是假数据)。
        #   修复: 本控件单独 objectName + 局部 QSS 压掉 min-width/padding/drop-down,
        #   宽度按字体像素实测, 不再依赖被 QSS 污染的 sizeHint。
        self.unit_combo = _CenterCombo()
        self.unit_combo.setObjectName("unitCombo")
        self._unit_mult = {"秒": 1, "分": 60, "时": 3600, "天": 86400}
        for text, mult in self._unit_mult.items():
            self.unit_combo.addItem(text, mult)
        self.unit_combo.setCurrentIndex(0)
        # (注意枚举名是 ...ContentsLengthWithIcon, 之前拼成 AdjustToMinimumContentsWithIcon
        #  直接 AttributeError, 导致整个自动间隔对话框弹不出来 — v44 修复)
        self.unit_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.unit_combo.setMinimumContentsLength(1)
        # 局部 QSS: 只覆盖这一个下拉(垂直 padding 沿用全局, 保持行高一致)。
        # 注意: 一旦样式表参与, QSS 盒模型的 min/max-width 比 QWidget 属性更权威,
        # 直接把目标宽度写进 QSS, 不再用 setFixedWidth(会被 min-width:0 压瘪成 8px)。
        _ufm = QFontMetrics(self.unit_combo.font())
        _longest = max(self._unit_mult, key=lambda t: _ufm.horizontalAdvance(t))
        # v53 调整: 整个 widget rect 都要当文字区, 让字在**整框几何中心**画。
        # 因此不再让 ::drop-down 占 16px 布局宽度(那个区会让字相对整框偏左 7px,
        # 是 v48~v52 一直居中不了的几何根因)。改为 ::drop-down 宽度 0,
        # 关闭态的下拉箭头由 _CenterCombo.paintEvent 在右边缘重叠画一个小三角
        # (不参与布局计算, 详见 _CenterCombo 注释)。
        # 文字 + 左右 padding(6) + 边框(2) + 余量(2) + 三角旁路空隙(10)
        _w = _ufm.horizontalAdvance(_longest) + 20
        self.unit_combo.setStyleSheet(
            f"#unitCombo {{ min-width: {_w}px; max-width: {_w}px;"
            f" padding-left: 4px; padding-right: 2px; }}"
            "#unitCombo::drop-down { width: 0px; border: none; }")
        # 下拉列表里的"秒/分/时/天"选项文字水平居中。
        # QComboBox 内部默认委托在 Windows 上忽略 model 的 TextAlignmentRole,
        # 所以 setItemData 不生效(v49)。本方案用 _CenterAlignDelegate 只覆写
        # initStyleOption 强制 displayAlignment=AlignCenter, paint 走基类完整链路,
        # 既不丢关闭态, 又能让 popup 列表真正居中。
        self.unit_combo.setItemDelegate(_CenterAlignDelegate(self.unit_combo))
        # 下拉里是单字(省空间), tooltip 显示完整单位名, 悬停可确认
        _unit_full = {"秒": "秒", "分": "分钟", "时": "小时", "天": "天"}
        _sync_unit_tip = lambda _=0: self.unit_combo.setToolTip(
            _unit_full.get(self.unit_combo.currentText(), ""))
        _sync_unit_tip()
        self.unit_combo.currentIndexChanged.connect(_sync_unit_tip)
        row1.addWidget(self.unit_combo)

        lbl_dir = QLabel("方向")
        lbl_dir.setFixedWidth(30)
        row1.addWidget(lbl_dir)

        # 方向: 两个 CheckCheckBox(蓝底+白勾 ✅) 用 QButtonGroup exclusive 做成互斥。
        # 视觉上一个 "带✓ 的格子" + 一行文字, 与现有 CheckCheckBox 风格保持一致;
        # 行为上仍互斥, 读取方向仍按 radio_asc.isChecked() 即可。
        self.radio_desc = CheckCheckBox("新→旧")
        self.radio_asc = CheckCheckBox("旧→新")
        self.radio_desc.setChecked(True)
        self._direction_group = QButtonGroup(self)
        self._direction_group.setExclusive(True)
        self._direction_group.addButton(self.radio_desc)
        self._direction_group.addButton(self.radio_asc)
        row1.addWidget(self.radio_desc)
        row1.addWidget(self.radio_asc)

        lay.addLayout(row1)

        # ---- 第二行: 三个时间 + ⓘ ----
        row2 = QHBoxLayout()
        row2.setSpacing(2)
        lbl_sync = QLabel("三个时间")
        # 宽度 = sizeHint + 余量。不能用 fm.advance 刚好贴合:
        # 汉字抗锯齿的右边缘会超出 advance(尤其"间"字门框), 贴合宽度会把"间"右侧切掉。
        # 也不能用 72(之前的值) — 右边空 24px, ⓘ 显得离文字很远。
        lbl_sync.setFixedWidth(lbl_sync.sizeHint().width() + 6)
        row2.addWidget(lbl_sync)

        _sync_tip = (
            "1. 三个时间完全一致\n"
            "2. 修改时间比拍摄晚 1 秒\n"
            "3. 只改拍摄时间")
        # _InfoLabel: 鼠标进入立即显示自定义弹窗(字号 17, 限最大宽 360, 移出即隐)
        info_btn = _InfoLabel(_sync_tip, size=14)
        row2.addWidget(info_btn)

        # 三个时间下拉: 按最长项"修改时间比拍摄晚 1 秒"的实际像素宽度
        # 设最小宽度(原来用 minimumContentsLength 字符数 + AdjustToMinimumContentsLengthWithIcon
        # 在 Windows 上对 CJK 字符宽度估算偏低, 显示不全)。这里用 QFontMetrics 精确算出。
        self.sync_combo = QComboBox()
        for key, text in timeplan.SYNC_MODES.items():
            self.sync_combo.addItem(text, key)
        self.sync_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        fm = QFontMetrics(self.sync_combo.font())
        longest = max(timeplan.SYNC_MODES.values(), key=lambda t: fm.horizontalAdvance(t))
        # 像素 = 最长项宽度 + 下拉箭头 (~18px) + 左右内边距 (~16px) + 一点冗余
        self.sync_combo.setMinimumWidth(fm.horizontalAdvance(longest) + 40)
        row2.addWidget(self.sync_combo)
        row2.addStretch(1)
        lay.addLayout(row2)

        self.preview = QLabel("")
        self.preview.setProperty("muted", True)
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            f"background: {T.SURFACE_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; padding: 10px;")
        lay.addWidget(self.preview)

        btns = QDialogButtonBox(QDialogButtonBox.Ok)
        btns.button(QDialogButtonBox.Ok).setText("应用")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self.unit_combo.currentIndexChanged.connect(lambda _=0: self._update_preview())
        self.value_spin.valueChanged.connect(lambda _=0: self._update_preview())
        self.radio_desc.toggled.connect(lambda _=0: self._update_preview())
        self.sync_combo.currentIndexChanged.connect(lambda _=0: self._update_preview())

    @property
    def step_seconds(self) -> float:
        mult = self.unit_combo.currentData() or 1
        return self.value_spin.value() * mult

    @property
    def direction(self) -> str:
        return "asc" if self.radio_asc.isChecked() else "desc"

    @property
    def sync_mode(self) -> str:
        return self.sync_combo.currentData() or "equal"

    @property
    def start_datetime(self) -> datetime:
        return self.start_segments.datetime()

    def _update_preview(self):
        count = len(self.photos)
        stamps = timeplan.compute_stamps(self.start_datetime, count,
                                         self.step_seconds, self.direction)
        if not stamps:
            self.preview.setText("没有可处理的照片")
            return
        first, last = stamps[0], stamps[-1]
        span = abs((last - first).total_seconds())
        self.preview.setText(
            f"第 1 张　{first:%Y-%m-%d %H:%M:%S}\n"
            f"第 {count} 张　{last:%Y-%m-%d %H:%M:%S}\n"
            f"跨度　{self._human_span(span)}")

    @staticmethod
    def _human_span(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f} 秒"
        if seconds < 3600:
            return f"{seconds / 60:.1f} 分钟"
        if seconds < 86400:
            return f"{seconds / 3600:.2f} 小时"
        return f"{seconds / 86400:.2f} 天"


class ChangePreviewDialog(_BaseDialog):
    """写入前的变更清单。"""

    def __init__(self, changes, photos_count: int, parent=None, rebuilder=None):
        super().__init__(parent, "写入前确认")
        self.changes = changes
        self._rebuild_handler = rebuilder
        self.resize(880, 580)
        self._build(photos_count)

    def _build(self, photos_count: int):
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        head = QLabel(f"共 <b>{photos_count}</b> 个文件、<b>{len(self.changes)}</b> 项改动。"
                      f"写入前会自动保存一份回滚快照。")
        head.setWordWrap(True)
        lay.addWidget(head)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["文件", "字段", "原值", "新值"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setRowCount(len(self.changes))
        for i, c in enumerate(self.changes):
            self.table.setItem(i, 0, QTableWidgetItem(os.path.basename(c.path)))
            self.table.setItem(i, 1, QTableWidgetItem(c.label))
            old = "（空）" if c.old in (None, "") else str(c.old)
            item_old = QTableWidgetItem(old)
            item_old.setForeground(T.TEXT_WEAK)
            self.table.setItem(i, 2, item_old)
            item_new = QTableWidgetItem(str(c.new))
            item_new.setForeground(T.ACCENT)
            font = item_new.font()
            font.setWeight(font.Weight.DemiBold)
            item_new.setFont(font)
            self.table.setItem(i, 3, item_new)
        lay.addWidget(self.table, 1)

        opts = QGroupBox("写入选项")
        ol = QVBoxLayout(opts)
        self.opt_thorough = QCheckBox("彻底模式：把文件里所有已有的时间字段一并统一")
        self.opt_xmp = QCheckBox("补写 XMP 时间字段（原文件没有 XMP 时创建）")
        self.opt_qt = QCheckBox("补写 QuickTime 时间字段（原文件没有时创建）")
        self.opt_inplace = QCheckBox("原地写入（不重写整个文件，视频大文件推荐）")
        self.opt_inplace.setChecked(True)
        ol.addWidget(self.opt_thorough)
        ol.addWidget(self.opt_xmp)
        ol.addWidget(self.opt_qt)
        ol.addWidget(self.opt_inplace)
        lay.addWidget(opts)

        warn = QLabel("这一步会真正修改硬盘上的文件。建议先在小批量上试一次，"
                      "确认手机相册顺序正确后再处理大批量。")
        warn.setProperty("weak", True)
        warn.setWordWrap(True)
        lay.addWidget(warn)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = btns.button(QDialogButtonBox.Ok)
        ok.setText("执行写入")
        ok.setProperty("accent", True)
        btns.button(QDialogButtonBox.Cancel).setText("再想想")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        for box in (self.opt_thorough, self.opt_xmp, self.opt_qt):
            box.stateChanged.connect(self._rebuild)

    def _rebuild(self):
        if self._rebuild_handler:
            self._rebuild_handler(self)

    def options(self) -> dict:
        return {
            "thorough": self.opt_thorough.isChecked(),
            "fill_xmp": self.opt_xmp.isChecked(),
            "fill_qt": self.opt_qt.isChecked(),
            "in_place": self.opt_inplace.isChecked(),
        }

    def refill(self, changes, photos_count: int):
        self.changes = changes
        self.table.setRowCount(len(changes))
        for i, c in enumerate(changes):
            self.table.setItem(i, 0, QTableWidgetItem(os.path.basename(c.path)))
            self.table.setItem(i, 1, QTableWidgetItem(c.label))
            old = "（空）" if c.old in (None, "") else str(c.old)
            self.table.setItem(i, 2, QTableWidgetItem(old))
            self.table.setItem(i, 3, QTableWidgetItem(str(c.new)))


class ProgressDialog(QDialog):
    """写入 / 回滚进度。"""

    canceled = Signal()

    def __init__(self, title: str, parent=None, cancelable: bool = True, *,
                 close_guard=None, minimizable: bool = False,
                 modal: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        # v197: 导出进度框专用 —— 关窗前先问一次要不要取消; 任务结束后不再问。
        self._close_guard = close_guard
        self._allow_close = False
        if minimizable:
            # ⚠️ 别用 ~Qt.XxxHint 去清位: 会连高位 WindowCloseButtonHint 一起清掉,
            #    系统标题栏那个 × 会变成灰的/点不动。用 int 按位或补上最小化即可。
            flags = int(self.windowFlags()) | int(Qt.WindowMinimizeButtonHint)
            self.setWindowFlags(Qt.WindowFlags(flags))
        lay = QVBoxLayout(self)
        self.label = QLabel("准备中…")
        lay.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(180)
        self.log.setStyleSheet(
            f"background: {T.SURFACE_ALT}; border: 1px solid {T.BORDER}; border-radius: 8px;")
        lay.addWidget(self.log, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        if cancelable:
            self.cancel_btn = QPushButton("取消")
            self.cancel_btn.clicked.connect(self.canceled.emit)
            row.addWidget(self.cancel_btn)
        else:
            self.cancel_btn = None
        self.close_btn = QPushButton("完成")
        self.close_btn.setProperty("accent", True)
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        # v222: 统一格式的进度框走非模态 —— 转码一跑几分钟, 没必要把整个主窗口
        # 锁死; 写入/回滚/合并仍是模态(它们与主窗口共享同一批文件, 锁着更安全)。
        if modal:
            self.setModal(True)

    def on_progress(self, done: int, total: int, name: str):
        self.bar.setMaximum(max(1, total))
        self.bar.setValue(done)
        self.label.setText(f"正在处理 {done} / {total}　{name}")

    def on_log(self, text: str, level: int = 0):
        color = {0: T.TEXT, 1: T.WARN, 2: T.DANGER}.get(level, T.TEXT)
        self.log.append(f'<span style="color:{color}">{text}</span>')

    def _guard_close(self) -> bool:
        """True = 放行关窗; False = 保持窗口。只有设了 close_guard 的进度框会问。"""
        if self._close_guard is None or self._allow_close:
            return True
        return self._close_guard(self) is True

    def closeEvent(self, event):
        """系统标题栏 × 走这里: 不放行就 ignore(), 窗口不会被关掉。"""
        if not self._guard_close():
            event.ignore()
            return
        self._allow_close = True       # 避免 super().closeEvent -> reject() 又问一次
        super().closeEvent(event)

    def reject(self):
        """Esc / 程序化 reject 是第二条关闭路径, 同样要过守卫。"""
        if not self._guard_close():
            return
        self._allow_close = True
        super().reject()

    def finish(self):
        self.bar.setValue(self.bar.maximum())
        if self.cancel_btn is not None:
            self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self._allow_close = True       # 任务已结束, 再点 × 不用问"取消导出"


class RollbackDialog(_BaseDialog):
    """快照列表与一键回滚。"""

    rollbackRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, "备份与回滚")
        self.resize(760, 460)
        lay = QVBoxLayout(self)
        tip = QLabel("每次写入前都会自动保存一份快照，记录被改字段的原始值。"
                     "选择任意一次，可以把那批文件还原到写入前的状态。")
        tip.setWordWrap(True)
        tip.setProperty("muted", True)
        lay.addWidget(tip)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["时间", "文件夹", "文件数", "改动项", "状态", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in (2, 3, 4, 5):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.refresh_btn = QPushButton("刷新")
        self.rollback_btn = QPushButton("回滚到这次")
        self.rollback_btn.setProperty("danger", True)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.rollback_btn)
        lay.addLayout(row)

        self.refresh_btn.clicked.connect(self.reload)
        self.rollback_btn.clicked.connect(self._do_rollback)
        self.reload()

    def reload(self):
        self.items = backup.list_snapshots()
        self.table.setRowCount(len(self.items))
        for i, item in enumerate(self.items):
            self.table.setItem(i, 0, QTableWidgetItem(item["created"].replace("T", " ")))
            self.table.setItem(i, 1, QTableWidgetItem(item["folder"]))
            self.table.setItem(i, 2, QTableWidgetItem(str(item["files"])))
            self.table.setItem(i, 3, QTableWidgetItem(str(item["changes"])))
            state = "已回滚" if item["rolled_back"] else "可用"
            st = QTableWidgetItem(state)
            st.setForeground(T.OK if not item["rolled_back"] else T.TEXT_WEAK)
            self.table.setItem(i, 4, st)
        if self.items:
            self.table.selectRow(0)

    def _do_rollback(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.items):
            return
        item = self.items[row]
        ret = QMessageBox.question(
            self, "确认回滚",
            f"将把 {item['files']} 个文件的 {item['changes']} 项字段还原到\n"
            f"{item['created']} 写入前的状态。\n\n确定继续吗？")
        if ret != QMessageBox.Yes:
            return
        self.rollbackRequested.emit(item["path"])
