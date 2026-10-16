# -*- coding: utf-8 -*-
"""列表自绘: 缩略图卡片 + 分组标题(含折叠三角与按月/按日开关)。"""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from . import theme as T
from ..core import fields as F
from ..core.photo import HeaderRow, PhotoRow, STATUS_DONE, STATUS_ERROR, STATUS_PENDING

SELECTED = QStyle.StateFlag.State_Selected
MOUSE_OVER = QStyle.StateFlag.State_MouseOver

CELL_W = 168
CELL_H = 202
PAD = 7
THUMB_H = CELL_W - PAD * 2
INFO_H = CELL_H - THUMB_H - PAD * 2

HEADER_H = 46
HEADER_INDENT = 26

SWITCH_W = 44
SWITCH_H = 24
SWITCH_GAP = 8


def _elide(text: str, width: int, font: QFont) -> str:
    return QFontMetrics(font).elidedText(text, Qt.ElideMiddle, width)


# ---------------------------------------------------------------- 分组标题
def header_layout(rect: QRect, row: HeaderRow) -> dict:
    """标题行各元素的矩形, 绘制与命中检测共用同一份坐标。"""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    indent = 12 + row.level * HEADER_INDENT
    out = {
        "arrow": QRect(x + indent, y + (h - 16) // 2, 16, 16),
        "toggles": {},
    }
    right = x + w - 12
    if row.level == 0:
        for key in ("day", "month"):
            out["toggles"][key] = QRect(right - SWITCH_W, y + (h - SWITCH_H) // 2,
                                        SWITCH_W, SWITCH_H)
            right -= SWITCH_W + SWITCH_GAP
    out["text_left"] = out["arrow"].right() + 10
    out["text_right"] = right
    return out


def hit_test_header(rect: QRect, row: HeaderRow, pos) -> str | None:
    lay = header_layout(rect, row)
    if lay["arrow"].contains(pos):
        return "collapse"
    for key, sw in lay["toggles"].items():
        if sw.contains(pos):
            return f"toggle:{key}"
    return None


def draw_switch(painter: QPainter, rect: QRect, text: str, on: bool, enabled: bool):
    # 和顶栏 SwitchButton 完全一致的绘制逻辑
    pill = rect.adjusted(1, 3, -1, -3)
    radius = pill.height() / 2
    path = QPainterPath()
    path.addRoundedRect(QRectF(pill), radius, radius)
    if not enabled:
        brush, pen, color = (QBrush(QColor("#F7F8FA")), QPen(QColor(T.BORDER), 1), QColor(T.TEXT_WEAK))
    elif on:
        brush, pen, color = (QBrush(QColor(T.ACCENT)), QPen(QColor(T.ACCENT), 1), QColor("#FFFFFF"))
    else:
        brush, pen, color = (QBrush(QColor("#F2F4F7")), QPen(QColor(T.BORDER_STRONG), 1), QColor(T.TEXT_SUB))
    # 关键: 先 setBrush/setPen 再 drawPath, 避免沿用调用方(paintEvent/draw_header)
    # 残留的当前画刷, 否则选中态会被画成灰色。
    painter.setBrush(brush)
    painter.setPen(pen)
    painter.drawPath(path)

    font = QFont()
    font.setPointSize(9)
    if on and enabled:
        font.setWeight(QFont.DemiBold)
    painter.setFont(font)
    painter.setPen(color)
    painter.drawText(rect, Qt.AlignCenter, text)


def draw_header(painter: QPainter, option, row: HeaderRow):
    rect = option.rect
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.fillRect(rect, QBrush(QColor(T.HEADER_BG)))

    lay = header_layout(rect, row)
    cy = rect.center().y()

    ax, ay = lay["arrow"].x(), lay["arrow"].y()
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor(T.TEXT_WEAK if row.level else T.TEXT_SUB)))
    tri = QPainterPath()
    if row.collapsed:
        tri.moveTo(ax + 5, ay + 4)
        tri.lineTo(ax + 12, ay + 8)
        tri.lineTo(ax + 5, ay + 12)
    else:
        tri.moveTo(ax + 4, ay + 5)
        tri.lineTo(ax + 12, ay + 5)
        tri.lineTo(ax + 8, ay + 12)
    tri.closeSubpath()
    painter.drawPath(tri)

    font = QFont()
    font.setPointSize(13 if row.level == 0 else 11)
    font.setWeight(QFont.DemiBold if row.level == 0 else QFont.Normal)
    painter.setFont(font)
    painter.setPen(QColor(T.TEXT if row.level == 0 else T.TEXT_SUB))
    title = row.label
    if row.level == 0 and isinstance(row.key[0], int):
        title = f"{row.key[0]} 年"
    tw = QFontMetrics(font).horizontalAdvance(title)
    painter.drawText(QRect(lay["text_left"], cy - 11, tw + 8, 22),
                     Qt.AlignLeft | Qt.AlignVCenter, title)

    font2 = QFont()
    font2.setPointSize(9)
    painter.setFont(font2)
    painter.setPen(QColor(T.TEXT_WEAK))
    count_text = f"{row.count} 张"
    painter.drawText(QRect(lay["text_left"] + tw + 10, cy - 9, 120, 18),
                     Qt.AlignLeft | Qt.AlignVCenter, count_text)

    if row.level == 0:
        draw_switch(painter, lay["toggles"]["month"], "月", row.month_on, row.month_enabled)
        draw_switch(painter, lay["toggles"]["day"], "日", row.day_on, row.day_enabled)

    if row.level == 0:
        painter.setPen(QPen(QColor(T.BORDER), 1))
        painter.drawLine(rect.x() + 12, rect.bottom(), rect.right() - 12, rect.bottom())
    painter.restore()


# ---------------------------------------------------------------- 照片卡片
def draw_photo(painter: QPainter, option, row: PhotoRow, selected: bool,
               hovered: bool, show_index: bool, ghost: bool = False, D=None):
    """ghost=True 表示这张卡片正在被拖动: 列表里保留它的位置(半透明),
    真正的"跟手"画面由悬浮在光标上的浮层显示。

    D 为所属 ThumbDelegate, 提供 cell_w/cell_h/thumb_h/info_h/horizontal/show_info,
    以便"显示方式"六档切换与 Ctrl+滚轮缩放复用同一套绘制逻辑。
    """
    photo = row.photo
    rect = option.rect
    cell_w = D.cell_w if D is not None else CELL_W
    cell_h = D.cell_h if D is not None else CELL_H
    pad = D.pad if D is not None else PAD
    thumb_h = D.thumb_h if D is not None else THUMB_H
    info_h = D.info_h if D is not None else INFO_H
    horizontal = D.horizontal if D is not None else False
    show_info = D.show_info if D is not None else True
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    if ghost:
        painter.setOpacity(0.25)

    outer = rect.adjusted(2, 2, -2, -2)
    base = QPainterPath()
    base.addRoundedRect(QRectF(outer), 10, 10)
    if selected:
        painter.fillPath(base, QBrush(QColor(T.ACCENT_SOFT)))
    elif hovered:
        painter.fillPath(base, QBrush(QColor("#F3F5F8")))

    if horizontal:
        n = max(14, cell_h - 8)
        icon_rect = QRect(outer.x() + 4, outer.y() + (cell_h - n) // 2, n, n)
    else:
        icon_rect = QRect(outer.x() + pad, outer.y() + pad, thumb_h, thumb_h)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(icon_rect), 8, 8)

    if photo.thumb and not photo.thumb.isNull():
        painter.save()
        painter.setClipPath(clip)
        pix = photo.thumb
        target = icon_rect.size() * 2
        scaled = pix.scaled(target, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        draw_rect = QRect(icon_rect)
        draw_rect.setSize(scaled.size() / 2)
        draw_rect.moveCenter(icon_rect.center())
        painter.drawPixmap(draw_rect, scaled)
        painter.restore()
    else:
        painter.fillPath(clip, QBrush(QColor(T.THUMB_BG)))
        painter.setPen(QPen(QColor("#D3DAE3"), 1))
        painter.drawPath(clip)
        f = QFont()
        f.setPointSize(9)
        painter.setFont(f)
        painter.setPen(QPen(QColor(T.TEXT_WEAK)))
        painter.drawText(icon_rect, Qt.AlignCenter, "视频" if photo.kind == "video" else "图片")

    if selected:
        painter.setPen(QPen(QColor(T.ACCENT), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(clip)

    if show_index:
        badge = QRect(icon_rect.x() + 6, icon_rect.y() + 6, 30, 20)
        bp = QPainterPath()
        bp.addRoundedRect(QRectF(badge), 6, 6)
        painter.fillPath(bp, QBrush(QColor(0, 0, 0, 132)))
        f = QFont()
        f.setPointSize(8)
        f.setWeight(QFont.DemiBold)
        painter.setFont(f)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(badge, Qt.AlignCenter, str(row.index))

    if photo.status in (STATUS_PENDING, STATUS_DONE, STATUS_ERROR):
        color = {STATUS_PENDING: T.WARN, STATUS_DONE: T.OK, STATUS_ERROR: T.DANGER}[photo.status]
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(color)))
        painter.drawEllipse(icon_rect.right() - 20, icon_rect.y() + 8, 10, 10)

    # 文字（文件名 + 拍摄时间）
    f = QFont()
    f.setPointSize(9)
    if horizontal:
        tx = icon_rect.right() + 8
        tw = outer.right() - tx - 6
        painter.setFont(f)
        painter.setPen(QColor(T.TEXT))
        painter.drawText(QRect(tx, outer.y() + 4, tw, 16),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         _elide(photo.name, tw, f))
        if cell_h >= 34 and show_info:
            planned = bool(photo.planned)
            stamp = photo.synthetic_display(F.TAKEN)
            f2 = QFont()
            f2.setPointSize(8)
            f2.setFamily(T.MONO_FAMILY)
            painter.setFont(f2)
            painter.setPen(QColor(T.ACCENT if planned else T.TEXT_WEAK))
            painter.drawText(QRect(tx, outer.y() + 4 + 16, tw, 14),
                             Qt.AlignLeft | Qt.AlignVCenter, stamp)
    else:
        info_w = thumb_h
        info = QRect(outer.x() + pad, icon_rect.bottom() + 4, info_w, info_h - 4)
        painter.setFont(f)
        painter.setPen(QColor(T.TEXT))
        painter.drawText(QRect(info.x(), info.y(), info.width(), 14),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         _elide(photo.name, info.width(), f))
        if show_info:
            planned = bool(photo.planned)
            stamp = photo.synthetic_display(F.TAKEN)
            f2 = QFont()
            f2.setPointSize(8)
            f2.setFamily(T.MONO_FAMILY)
            painter.setFont(f2)
            painter.setPen(QColor(T.ACCENT if planned else T.TEXT_WEAK))
            painter.drawText(QRect(info.x(), info.y() + 14, info.width(), 13),
                             Qt.AlignLeft | Qt.AlignVCenter, stamp)

    painter.restore()


class ThumbDelegate(QStyledItemDelegate):
    """自绘委托。header 的宽度撑满视口, 从而在自动换行布局里独占一行。

    cell_w / cell_h / thumb_h / info_h 可被 set_cell() 改变, 实现主网格的
    "显示方式"六档切换与 Ctrl+滚轮缩放。horizontal=True 时改为"图标在左、文字在右"
    的横向列表样式（用于"列表"与"详细信息"两档）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.show_index = True
        self.hover_row = -1
        # 可配置的网格尺寸（默认与原来的 CELL_W/CELL_H 一致）
        self.cell_w = CELL_W
        self.cell_h = CELL_H
        self.pad = PAD
        self.thumb_h = THUMB_H
        self.info_h = INFO_H
        self.horizontal = False
        self.show_info = True

    def set_cell(self, w: int, h: int, horizontal: bool = False, show_info: bool = True):
        """更新网格尺寸并派生缩略图/信息区高度。"""
        self.cell_w = w
        self.cell_h = h
        self.horizontal = horizontal
        self.show_info = show_info
        self.thumb_h = max(16, w - PAD * 2)
        self.info_h = max(8, h - self.thumb_h - PAD * 2)

    def sizeHint(self, option, index) -> QSize:
        from ..core.photo import PhotoModel
        row = index.data(PhotoModel.ROLE_ROW)
        if isinstance(row, HeaderRow):
            width = 600
            view = self.parent()
            if view is not None and hasattr(view, "viewport"):
                width = max(320, view.viewport().width() - 2)
            return QSize(width, HEADER_H)
        return QSize(self.cell_w, self.cell_h)

    def paint(self, painter, option, index):
        from ..core.photo import PhotoModel
        row = index.data(PhotoModel.ROLE_ROW)
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if isinstance(row, HeaderRow):
            draw_header(painter, option, row)
        else:
            # 正在被拖动的卡片: 在列表原位画成半透明, 视觉上告诉用户"这里会空出来"
            view = self.parent()
            dragging = getattr(view, "live_ids", None)
            ghost = bool(dragging) and id(row.photo) in dragging
            draw_photo(painter, option, row,
                       bool(option.state & SELECTED),
                       bool(option.state & MOUSE_OVER) or self.hover_row == index.row(),
                       self.show_index, ghost, self)
        painter.restore()
