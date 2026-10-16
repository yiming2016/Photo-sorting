# -*- coding: utf-8 -*-
"""自绘小部件: 胶囊开关 / 勾选框。"""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QRubberBand, QStyle,
                               QStyleOptionButton)

from . import theme as T


class Marquee(QRubberBand):
    """自绘的框选矩形(鼠标拉框)。

    为什么不用系统自带的那一个: 全局 QSS 里有 `QWidget { background: transparent; }`,
    Qt 的橡皮筋是按 QWidget 参与样式计算的, 于是被画成透明 —— 拉框时能看到"选中了",
    却看不到框本身。这里直接接管 paintEvent, 不依赖任何风格, 保证一定可见。
    """

    def __init__(self, parent=None):
        super().__init__(QRubberBand.Rectangle, parent)
        # 不能吃掉鼠标事件, 否则下面的视图收不到 move/release
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.fillRect(r, QColor(T.ACCENT + "2B"))          # 半透明蓝底
        p.setPen(QPen(QColor(T.ACCENT), 1))             # 蓝色实线边框
        p.setBrush(Qt.NoBrush)
        p.drawRect(r)
        p.end()


class SwitchButton(QAbstractButton):
    """胶囊开关: 开=蓝底白字, 关=灰底灰字, 禁用=更淡。"""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text = text
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(26)

    def setText(self, text: str):
        self._text = text
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(QFont())
        return QSize(fm.horizontalAdvance(self._text) + 26, 26)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(1, 3, -1, -3)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), rect.height() / 2, rect.height() / 2)
        on = self.isChecked()
        enabled = self.isEnabled()
        if not enabled:
            painter.fillPath(path, QBrush(QColor("#F7F8FA")))
            painter.setPen(QPen(QColor(T.BORDER), 1))
            color = QColor(T.TEXT_WEAK)
        elif on:
            painter.fillPath(path, QBrush(QColor(T.ACCENT)))
            painter.setPen(QPen(QColor(T.ACCENT), 1))
            color = QColor("#FFFFFF")
        else:
            painter.fillPath(path, QBrush(QColor("#F2F4F7")))
            painter.setPen(QPen(QColor(T.BORDER_STRONG), 1))
            color = QColor(T.TEXT_SUB)
        painter.drawPath(path)

        font = QFont()
        font.setPointSize(9)
        if on and enabled:
            font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(self.rect(), Qt.AlignCenter, self._text)
        painter.end()


def set_accent(widget, on: bool = True):
    widget.setProperty("accent", "true" if on else "false")
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class CheckCheckBox(QCheckBox):
    """QCheckBox: 选中时在 indicator 区域覆盖绘制蓝色圆角背景 + 白色对勾,
    解决 Fusion 风格下 QSS image:/color: 对 indicator 勾选标记无效的问题。"""

    def paintEvent(self, event):
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 先让 Fusion 绘制完整 checkbox（文字、间距、未选中状态等）
        self.style().drawControl(QStyle.CE_CheckBox, opt, p, self)
        # 选中时覆盖绘制蓝底+白勾
        if self.isChecked():
            ir = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, self)
            if not ir.isEmpty():
                p.save()
                r = ir.adjusted(1, 1, -1, -1)
                # 蓝色圆角背景
                bg = QPainterPath()
                bg.addRoundedRect(QRectF(r), 3, 3)
                p.fillPath(bg, QBrush(QColor(T.ACCENT)))
                # 白色对勾
                pen = QPen(QColor("#FFFFFF"))
                pen.setWidthF(2.0)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                tick = QPainterPath()
                tick.moveTo(r.x() + 3, r.y() + r.height() * 0.55)
                tick.lineTo(r.x() + r.width() * 0.4, r.y() + r.height() * 0.75)
                tick.lineTo(r.x() + r.width() - 3, r.y() + 3)
                p.drawPath(tick)
                p.restore()
        p.end()
