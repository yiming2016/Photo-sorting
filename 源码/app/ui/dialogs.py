# -*- coding: utf-8 -*-
"""各类对话框: 自动间隔、变更清单、回滚、设置。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QKeyEvent, QPainter,
                           QPainterPath)
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QButtonGroup,
                               QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QProgressBar, QPushButton, QSpinBox, QStyle,
                               QStyledItemDelegate, QStyleOptionComboBox,
                               QTableWidget, QTableWidgetItem, QTextEdit, QToolTip,
                               QVBoxLayout, QWidget)

from . import theme as T
from .widgets import CheckCheckBox
from ..core import backup, timeplan
from ..core.photo import Photo


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
      弹窗里当前项未必带 HasFocus 状态), 高亮 + 右侧画一个对勾, 明确"这是当前选中的"。

    combo: 所归属的 QComboBox, 用于判定当前选中项(构造时传入, 见 _CenterCombo)。
    """

    def __init__(self, combo=None, parent=None):
        super().__init__(parent)
        self._combo = combo

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
        # 当前选中项: 右侧画一个对勾(画在基类之上, 不被覆盖)
        if current:
            painter.save()
            painter.setPen(QColor(T.ACCENT))
            f = QFont(option.font)
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(QRect(option.rect).adjusted(0, 0, -10, 0),
                             Qt.AlignRight | Qt.AlignVCenter, "✓")
            painter.restore()


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
        # 2. 在整个 widget rect 里 AlignCenter 画当前文字(整框几何中心 = 23.5/16)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect(), Qt.AlignCenter, self.currentText())
        painter.end()


def confirm_destructive(parent, title: str, text: str) -> bool:
    """二次确认框: 左「否」右「是」，全中文、无任何 Y/N 英文，「是」按钮为红色。

    - 默认焦点落在「否」：按 Enter 或 Esc 都 = 否（安全默认，避免误删）。
    - 用于「从列表移除」「永久删除文件」等危险操作。
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(420)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(14)
    msg = QLabel(text)
    msg.setWordWrap(True)
    msg.setProperty("muted", True)
    lay.addWidget(msg, 1)

    row = QHBoxLayout()
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
    lay.addLayout(row)

    dlg.setModal(True)
    no_btn.clicked.connect(dlg.reject)
    yes_btn.clicked.connect(dlg.accept)
    return dlg.exec() == QDialog.DialogCode.Accepted


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
        self.setRange(0.01, 999999)
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

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
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
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.canceled.emit)
        self.close_btn = QPushButton("完成")
        self.close_btn.setProperty("accent", True)
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.setModal(True)

    def on_progress(self, done: int, total: int, name: str):
        self.bar.setMaximum(max(1, total))
        self.bar.setValue(done)
        self.label.setText(f"正在处理 {done} / {total}　{name}")

    def on_log(self, text: str, level: int = 0):
        color = {0: T.TEXT, 1: T.WARN, 2: T.DANGER}.get(level, T.TEXT)
        self.log.append(f'<span style="color:{color}">{text}</span>')

    def finish(self):
        self.bar.setValue(self.bar.maximum())
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)


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
