# -*- coding: utf-8 -*-
"""剪辑工具条图标: 纯 QPainter 画，不依赖 QtSvg。

为什么自己画:
- 打包时少带一个 QtSvg 插件, 少一类"本机图片能跑 exe 里白块"的风险;
- 图标只有十来个, 全是简单几何, 画出来比找图标库更可控;
- 颜色由调用方给, 跟着主题走, 不会在深色/浅色下变成看不见的黑块。

坐标一律按 20×20 的格子写, 输出时整体缩放。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap


def _poly(*pts) -> list:
    return [QPointF(x, y) for x, y in pts]


def _pen(color: str, w: float = 1.6):
    pen = QPen(QColor(color), w)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _play(p: QPainter):
    p.setBrush(QColor(p.pen().color()))
    p.drawPolygon(_poly((6, 4), (6, 16), (16, 10)))
    p.setBrush(Qt.NoBrush)


def _pause(p: QPainter):
    p.drawLine(7, 4, 7, 16)
    p.drawLine(13, 4, 13, 16)


def _prev(p: QPainter):
    p.setBrush(QColor(p.pen().color()))
    p.drawPolygon(_poly((8, 5), (8, 15), (3, 10)))
    p.setBrush(Qt.NoBrush)
    p.drawLine(14, 5, 14, 15)


def _next(p: QPainter):
    p.setBrush(QColor(p.pen().color()))
    p.drawPolygon(_poly((12, 5), (12, 15), (17, 10)))
    p.setBrush(Qt.NoBrush)
    p.drawLine(6, 5, 6, 15)


def _split(p: QPainter):
    p.drawLine(10, 3, 10, 17)
    p.setBrush(QColor(p.pen().color()))
    p.drawPolygon(_poly((5, 10), (8, 7), (8, 13)))
    p.drawPolygon(_poly((15, 10), (12, 7), (12, 13)))
    p.setBrush(Qt.NoBrush)


def _delete(p: QPainter):
    p.drawLine(8, 3, 12, 3)
    p.drawLine(5, 5, 15, 5)
    p.drawLine(6, 6, 7, 17)
    p.drawLine(14, 6, 13, 17)
    p.drawLine(7, 17, 13, 17)
    p.drawLine(9, 8, 9, 15)
    p.drawLine(11, 8, 11, 15)


def _add(p: QPainter):
    p.drawLine(10, 4, 10, 16)
    p.drawLine(4, 10, 16, 10)


def _trim(p: QPainter):
    p.drawLine(8, 2, 3, 10)
    p.drawLine(3, 10, 8, 18)
    p.drawLine(12, 2, 17, 10)
    p.drawLine(17, 10, 12, 18)


def _zoom_in(p: QPainter):
    p.drawEllipse(4, 4, 11, 11)
    p.drawLine(12, 12, 17, 17)
    p.drawLine(7, 9.5, 12, 9.5)
    p.drawLine(9.5, 7, 9.5, 12)


def _zoom_out(p: QPainter):
    p.drawEllipse(4, 4, 11, 11)
    p.drawLine(12, 12, 17, 17)
    p.drawLine(7, 9.5, 12, 9.5)


def _fit(p: QPainter):
    p.drawLine(3, 10, 17, 10)
    p.drawLine(3, 10, 7, 6)
    p.drawLine(3, 10, 7, 14)
    p.drawLine(17, 10, 13, 6)
    p.drawLine(17, 10, 13, 14)


def _export(p: QPainter):
    p.drawLine(3, 5, 11, 5)
    p.drawLine(3, 5, 3, 15)
    p.drawLine(3, 15, 11, 15)
    p.drawLine(9, 10, 17, 10)
    p.drawLine(13, 7, 17, 10)
    p.drawLine(13, 13, 17, 10)


def _gear(p: QPainter):
    """齿轮: 8 个短齿 + 两个同心圆。

    为什么自己画: "⚙"(U+2699) 在微软雅黑 / Segoe UI Symbol 里都没有字形,
    按钮上会渲染成一个空心方块(tofu), 看着像坏掉了。
    """
    cx = cy = 10.0
    for i in range(8):
        a = math.radians(i * 45.0)
        ca, sa = math.cos(a), math.sin(a)
        p.drawLine(QPointF(cx + 6.1 * ca, cy + 6.1 * sa),
                   QPointF(cx + 8.6 * ca, cy + 8.6 * sa))
    p.drawEllipse(QPointF(cx, cy), 6.1, 6.1)
    p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)


def _help(p: QPainter):
    f = QFont()
    f.setPointSize(13)
    f.setBold(True)
    p.setFont(f)
    p.drawText(0, 0, 20, 20, Qt.AlignCenter, "?")


def _camera(p: QPainter):
    """照相机: 机身 + 顶部取景凸起 + 中间镜头。

    为什么不用 "📷"(U+1F4F7): 那个是彩色 emoji, 收不到主题色,
    而且在没有彩色字体的机器上会退成一个黑白豆腐块。
    """
    # 机身
    p.drawRoundedRect(2.5, 6.0, 15.0, 10.5, 2.0, 2.0)
    # 顶棚(取景器凸起)
    p.drawLine(7.2, 6.0, 8.4, 3.9)
    p.drawLine(8.4, 3.9, 11.6, 3.9)
    p.drawLine(11.6, 3.9, 12.8, 6.0)
    # 镜头
    p.drawEllipse(QPointF(10.0, 11.2), 3.1, 3.1)
    p.drawEllipse(QPointF(10.0, 11.2), 1.3, 1.3)


_DRAW = {
    "play": _play, "pause": _pause, "prev": _prev, "next": _next,
    "split": _split, "delete": _delete, "add": _add, "trim": _trim,
    "zoom_in": _zoom_in, "zoom_out": _zoom_out, "fit": _fit,
    "export": _export, "help": _help, "gear": _gear, "camera": _camera,
}


def make_icon(name: str, color: str = "#5F5E5A", size: int = 20) -> QIcon:
    """按 20×20 的格子画一个图标, 缩放到 size 输出。"""
    fn = _DRAW.get(name)
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    if fn is None:
        return QIcon(pm)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(_pen(color, 1.6 * size / 20.0))
    p.setBrush(Qt.NoBrush)
    p.scale(size / 20.0, size / 20.0)
    fn(p)
    p.end()
    return QIcon(pm)
