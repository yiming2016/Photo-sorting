# -*- coding: utf-8 -*-
"""列表自绘: 缩略图卡片 + 分组标题(含折叠三角与按月/按日开关)。"""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QStyle, QStyledItemDelegate, QStyleOptionViewItem)

from . import theme as T
from ..core import fields as F
from ..core.photo import (CollapsedPack, HeaderRow, PackRow, PhotoRow, RowBreak,
                          STATUS_DONE, STATUS_ERROR, STATUS_PENDING)

SELECTED = QStyle.StateFlag.State_Selected
MOUSE_OVER = QStyle.StateFlag.State_MouseOver

CELL_W = 168
CELL_H = 202
PAD = 7
THUMB_H = CELL_W - PAD * 2
INFO_H = CELL_H - THUMB_H - PAD * 2

HEADER_H = 32
HEADER_INDENT = 26

SWITCH_W = 44
SWITCH_H = 24
SWITCH_GAP = 8

# 合并模式组标题上的"全选本组"勾选框边长
CHECK_BOX = 18


def _elide(text: str, width: int, font: QFont) -> str:
    return QFontMetrics(font).elidedText(text, Qt.ElideMiddle, width)


def _switch_w(text: str) -> int:
    """组标题上的胶囊开关宽度: 单字(月/日)固定 44, 多字(分辨率/帧率)按字宽加左右内边距。"""
    if len(text) <= 1:
        return SWITCH_W
    f = QFont()
    f.setPointSize(9)
    return max(SWITCH_W, QFontMetrics(f).horizontalAdvance(text) + 20)


# 组标题右端胶囊开关的文案(排序=月/日, 统一=分辨率/帧率)
HEADER_SWITCH_TEXT = {"month": "月", "day": "日", "res": "分辨率", "fps": "帧率"}


# ---------------------------------------------------------------- 分组标题
def header_layout(rect: QRect, row: HeaderRow) -> dict:
    """标题行各元素的矩形, 绘制与命中检测共用同一份坐标。"""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    # v239: 用户直接指定三级缩进绝对值 —— 一级=0、二级=6、三级(满宽叶组)=12。
    # 排序=年/月/日、统一=编码/分辨率/帧率; 单文件叶组走 pack_layout 不在这里。
    # 绘制(draw_header)与命中(hit_test_header)共用本函数, 改一次两者自动同步。
    if row.level == 0:
        indent = 0
    elif row.level == 1:
        indent = 6
    else:
        indent = 12
    out = {
        "arrow": QRect(x + indent, y + (h - 16) // 2, 16, 16),
        "toggles": {},
    }
    right = x + w - 12
    if row.level == 0 and not row.merge_tag:
        # 排序是"年"行上的 月/日; 统一是"编码"行上的 分辨率/帧率(同一个位置)
        keys = ("fps", "res") if row.norm_mode else ("day", "month")
        for key in keys:
            sw = _switch_w(HEADER_SWITCH_TEXT.get(key, ""))
            out["toggles"][key] = QRect(right - sw, y + (h - SWITCH_H) // 2,
                                        sw, SWITCH_H)
            right -= sw + SWITCH_GAP
    out["text_left"] = out["arrow"].right() + 10
    # 每个分组标题前面都放一个"全选本组"勾选框: 合并模式按规格组, 排序模式按
    # 年 / 月 / 日(用户要求"年份前面也要有一个能全选的 ✅")。文字统一往后让位。
    out["check"] = QRect(out["text_left"], y + (h - CHECK_BOX) // 2,
                         CHECK_BOX, CHECK_BOX)
    out["text_left"] = out["check"].right() + 9
    out["text_right"] = right
    return out


# 合并模式组标题的标记色: 可无损=蓝 / 需转码=橙 / 独一个=灰
_MERGE_TAG_COLOR = {"lossless": "ACCENT", "transcode": "WARN", "single": "TEXT_WEAK"}


def norm_need_text(need: int) -> str:
    """统一页组标题的达标提示(满宽标题 / 紧凑卡共用单一来源)。
    v241: 原 "N 个需转码" 按用户要求精简为 "N个"。"""
    return f"{need}个" if need > 0 else "可直接快合"


def hit_test_header(rect: QRect, row: HeaderRow, pos) -> str | None:
    """组标题的命中: 勾选框 / 胶囊开关各有各的, 其余整行都是"收起-展开"。

    v218: 以前只有倒三角(含四周 6px 宽容)能折叠, 标题文字和右侧空白点了没反应,
    用户要求"点整组就能收起"。所以兜底不再返回 None, 而是 "collapse" ——
    整行标题(除勾选框、胶囊开关外)都能折叠/展开, 排序/合并/统一三态一致。
    """
    lay = header_layout(rect, row)
    # 三角本身只有 16×16, 往四周放一点(共 ~28×32), 免得"明明点在三角上却没反应"。
    # ⚠️ 别往右放太多: 再往右就是"全选本组"的勾选框 / 组标题文字, 会抢它的点击。
    if lay["arrow"].adjusted(-6, -8, 6, 8).contains(pos):
        return "collapse"
    chk = lay.get("check")
    if chk is not None and chk.adjusted(-4, -6, 4, 6).contains(pos):
        return "groupcheck"
    for key, sw in lay["toggles"].items():
        if sw.contains(pos):
            return f"toggle:{key}"
    return "collapse"


def draw_check_box(painter: QPainter, rect: QRect, state: int):
    """组标题上的勾选框。state: 0=未选 / 1=部分选中(横杠) / 2=整组选中(对勾)。"""
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), 5, 5)
    if state <= 0:
        painter.setPen(QPen(QColor("#B9C2CE"), 1.4))
        painter.setBrush(QBrush(QColor("#FFFFFF")))
        painter.drawPath(path)
        return
    painter.setPen(QPen(QColor(T.ACCENT), 1.4))
    painter.setBrush(QBrush(QColor(T.ACCENT)))
    painter.drawPath(path)
    pen = QPen(QColor("#FFFFFF"), 2.1)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    cy = rect.center().y()
    if state == 1:
        painter.drawLine(rect.x() + 4, cy, rect.right() - 4, cy)
        return
    tp = QPainterPath()
    tp.moveTo(rect.x() + 4.2, cy + 0.6)
    tp.lineTo(rect.x() + 7.4, rect.bottom() - 4.8)
    tp.lineTo(rect.right() - 3.8, rect.y() + 4.8)
    painter.drawPath(tp)


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

    # v224: 最大的分组(排序的"年" / 合并的规格组 / 统一的编码组)上缘画一条彩色粗线,
    # 让顶层分组之间的分隔明显区别于组标题底部那条灰色细线。
    if row.level == 0:
        painter.setPen(QPen(QColor(T.ACCENT), 3))
        painter.drawLine(rect.x() + 12, rect.y() + 1, rect.right() - 12, rect.y() + 1)

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

    if row.norm_mode:
        # ---- 统一页三级分组标题(编码→分辨率→帧率): 只写该级名 + "N个" ----
        draw_check_box(painter, lay["check"], getattr(row, "group_checked", 0))
        x = lay["text_left"]
        painter.setPen(QColor(T.TEXT))
        tw = QFontMetrics(font).horizontalAdvance(row.label)
        painter.drawText(QRect(x, cy - 11, tw + 8, 22),
                         Qt.AlignLeft | Qt.AlignVCenter, row.label)
        x += tw + 12
        f2 = QFont()
        f2.setPointSize(10)
        painter.setFont(f2)
        note = norm_need_text(row.norm_need)
        painter.setPen(QColor(T.WARN if row.norm_need > 0 else T.ACCENT))
        nw = QFontMetrics(f2).horizontalAdvance(note)
        painter.drawText(QRect(x, cy - 9, nw + 8, 18),
                         Qt.AlignLeft | Qt.AlignVCenter, note)
        # v244: 分辨率级标题在"N个"右侧补 (横屏)/(竖屏), 只该级有这个字段
        orient = getattr(row, "norm_orientation", "") or ""
        if orient:
            otxt = f"({orient})"
            ow = QFontMetrics(f2).horizontalAdvance(otxt)
            painter.setPen(QColor(T.TEXT_WEAK))
            painter.drawText(QRect(x + nw + 8, cy - 9, ow + 8, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, otxt)
        # 编码级标题右端: 分辨率/帧率开关(与排序"年"行上的 月/日 同一个位置)
        if row.level == 0:
            draw_switch(painter, lay["toggles"]["res"], "分辨率",
                        row.norm_res_on, row.norm_res_enabled)
            draw_switch(painter, lay["toggles"]["fps"], "帧率",
                        row.norm_fps_on, row.norm_fps_enabled)
        painter.setPen(QPen(QColor(T.BORDER), 1))
        painter.drawLine(rect.x() + 12, rect.bottom(), rect.right() - 12, rect.bottom())
        painter.restore()
        return

    if row.merge_tag:
        # ---- 合并模式组标题: [✓] 可无损合并  3 段  h264 · 1920×1080 ----
        color = getattr(T, _MERGE_TAG_COLOR.get(row.merge_tag, "TEXT_WEAK"))
        draw_check_box(painter, lay["check"], getattr(row, "group_checked", 0))
        x = lay["text_left"]
        painter.setPen(QColor(T.TEXT))
        tw = QFontMetrics(font).horizontalAdvance(row.label)
        painter.drawText(QRect(x, cy - 11, tw + 8, 22),
                         Qt.AlignLeft | Qt.AlignVCenter, row.label)
        x += tw + 12
        font2 = QFont()
        font2.setPointSize(9)
        painter.setFont(font2)
        cnt = f"{row.count} 段"
        cw = QFontMetrics(font2).horizontalAdvance(cnt)
        if row.count > 1:      # v226: 单文件组不再写"1 段"(组名"独立"已说明一切)
            painter.setPen(QColor(color))
            painter.drawText(QRect(x, cy - 9, cw + 6, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, cnt)
            x += cw + 12
        parts = getattr(row, "merge_parts", []) or []
        avail = max(20, lay["text_right"] - x)
        fm2 = QFontMetrics(font2)
        if parts:
            # 规格串只写值(不写参数名): 值按致命度常驻标色(硬致命红/软致命橙)。
            painter.setFont(font2)
            cur = x
            first = True
            sep = " · "
            sep_w = fm2.horizontalAdvance(sep)
            for val, lv in parts:
                if not first:
                    if cur + sep_w > lay["text_right"] - 4:
                        break
                    painter.setPen(QColor(T.TEXT_WEAK))
                    painter.drawText(QRect(cur, cy - 9, sep_w + 2, 18),
                                     Qt.AlignLeft | Qt.AlignVCenter, sep)
                    cur += sep_w
                first = False
                vcolor = (QColor(T.DANGER) if lv == "fatal"
                          else QColor(T.WARN) if lv == "warn"
                          else QColor(T.TEXT_WEAK))
                vw = fm2.horizontalAdvance(val)
                if cur + vw > lay["text_right"] - 4:
                    break
                painter.setPen(vcolor)
                painter.drawText(QRect(cur, cy - 9, vw + 2, 18),
                                 Qt.AlignLeft | Qt.AlignVCenter, val)
                cur += vw
        elif row.merge_note:
            painter.setPen(QColor(T.TEXT_WEAK))
            painter.drawText(QRect(x, cy - 9, avail, 18),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             fm2.elidedText(row.merge_note, Qt.ElideRight, avail))
        painter.setPen(QPen(QColor(T.BORDER), 1))
        painter.drawLine(rect.x() + 12, rect.bottom(), rect.right() - 12, rect.bottom())
        painter.restore()
        return

    # 排序模式(年 / 月 / 日): 标题前面也有"全选本组"勾选框, 和合并模式长得一样
    draw_check_box(painter, lay["check"], getattr(row, "group_checked", 0))

    painter.setPen(QColor(T.TEXT if row.level == 0 else T.TEXT_SUB))
    title = row.label
    if row.level == 0 and row.key and isinstance(row.key[0], int):
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


# ---------------------------------------------------------------- 紧凑子组卡(v226)
# 单文件子组: 组头 + 唯一照片合成一个格子(宽 = 一个卡片格), 一行可并排多个小分组。
# 组头信息按卡宽折行绘制, 内容一项不丢(规格值保留原致命度配色)。
PACK_X = 12             # 卡内左距(v240: 原 4 → 12, 使三级单文件卡三角 x=14 与三级满宽标题一致,
                        # 满足用户"一级<二级<三级"越深越靠右; 改的是全局常量, 4 处引用自动同步)
PACK_ARROW_W = 16
PACK_CHK_GAP = 6
PACK_TEXT_GAP = 8
PACK_LINE_H = 20        # 每行文字高度(pt11 与 pt9 混排, 统一按 20px 行框居中)
PACK_PAD_TOP = 4
PACK_PAD_BOTTOM = 5


def pack_layout(head_rect: QRect, hdr: HeaderRow) -> dict:
    """紧凑组头首行(三角/勾选框)的矩形 —— 绘制与命中检测共用同一份坐标。

    v228: 文本一律**顶部锚定**(off = PACK_PAD_TOP)。v227 的 bottom-anchor 在
    同排卡片折行数不同时会把首行"独立"压得高低不一(帧率串更宽的卡多折一行,
    首行就矮一截) —— 用户要求"文字格式也统一"; 统一头高不变, 照片区仍然水平,
    只是矮卡头的富余空间从"顶上"挪到了"底下"。
    """
    fy = head_rect.y() + PACK_PAD_TOP
    return {
        "arrow": QRect(head_rect.x() + PACK_X,
                       fy + (PACK_LINE_H - 16) // 2,
                       PACK_ARROW_W, 16),
        "check": QRect(head_rect.x() + PACK_X + PACK_ARROW_W + PACK_CHK_GAP,
                       fy + (PACK_LINE_H - CHECK_BOX) // 2,
                       CHECK_BOX, CHECK_BOX),
        "text_left": head_rect.x() + PACK_X + PACK_ARROW_W + PACK_CHK_GAP
                     + CHECK_BOX + PACK_TEXT_GAP,
        "text_y0": fy,
    }


# 续行(第 2 行起)的起点 = 勾选框左缘下方: 规格文字"居左到头"(v228 用户口径:
# 选中方块下面就是最左)。首行(组名)仍在勾选框右侧。
PACK_CONT_X = PACK_X + PACK_ARROW_W + PACK_CHK_GAP


def pack_lines(row: HeaderRow, width: int) -> list:
    """紧凑组头的折行结果: [[(text, color_key, size, demi, lv), ...], ...]。

    绘制与测高(pack_header_h)共用同一份折行, 保证高度和实际画出的一致。
    合并单文件组(v227 口径): 首行 = 组名(左) + 编码格式(右, 放不下才折行),
    其余规格段从下一行起**居左**流式排列; 组名是"独立"、单文件组不写"1 段"。
    """
    x0 = PACK_X + PACK_ARROW_W + PACK_CHK_GAP + CHECK_BOX + PACK_TEXT_GAP
    avail = max(40, width - x0 - PACK_X)
    cont_avail = max(40, width - PACK_CONT_X - PACK_X)
    gap = 6

    def flow(segs, first_x):
        """折行: 首行从 first_x 起(右边界 = 首行可用宽), 之后每行统一从
        PACK_CONT_X 起(勾选框下方) —— 与 draw_pack 的实际绘制坐标一致。
        v228: 单段文字宽过整张卡时省略号截断 —— 小图标档(cell_w 很小)下
        规格串会横向溢出画进右边邻卡(两张卡的文字叠在一起), 绝不允许。"""
        lines, cur = [], []
        cur_x, cap = first_x, first_x + avail
        for seg in segs:
            text, ck_, size, demi, lv = seg
            f = _pack_font(size, demi)
            fm = QFontMetrics(f)
            tw = fm.horizontalAdvance(text)
            if cur and cur_x + tw > cap:
                lines.append(cur)
                cur, cur_x = [], PACK_CONT_X
                cap = PACK_CONT_X + cont_avail
            # 兜底: 这一段从 cur_x 起已经画出了卡片右缘 → 截断到卡缘为止
            room = width - cur_x
            if tw > room:
                text = fm.elidedText(text, Qt.ElideRight, max(8, room))
                tw = fm.horizontalAdvance(text)
            cur.append((text, ck_, size, demi, lv))
            cur_x += tw + gap
        if cur:
            lines.append(cur)
        return lines

    if row.merge_tag:
        label_seg = (row.label, "TEXT", 11, True, None)
        count_segs = []
        if row.count > 1:
            count_segs.append((f"{row.count} 段",
                               _MERGE_TAG_COLOR.get(row.merge_tag, "TEXT_WEAK"),
                               9, False, None))
        parts = list(getattr(row, "merge_parts", None) or [])
        if parts:
            cv, clv = parts[0]          # 第一段 = 编码格式
            codec_seg = (str(cv), None, 9, False, clv)
            head_w = (_pack_segs_width([label_seg] + count_segs) + gap
                      + _pack_segs_width([codec_seg]))
            rest = [(str(v), None, 9, False, lv) for v, lv in parts[1:]]
            if head_w <= avail:
                # 首行: 组名靠左 + 编码格式贴右缘; 其余段下一行起居左(勾选框下方)
                return [[label_seg] + count_segs + [codec_seg]] + flow(rest, PACK_CONT_X)
            return flow([label_seg] + count_segs + [codec_seg] + rest, x0)
        segs = [label_seg] + count_segs
        if row.merge_note:
            segs.append((row.merge_note, "TEXT_WEAK", 9, False, None))
        return flow(segs, x0)
    if row.norm_mode:
        segs = [(row.label, "TEXT_SUB", 11, False, None)]
        segs.append((norm_need_text(row.norm_need),
                     "WARN" if row.norm_need > 0 else "ACCENT", 9, False, None))
        # v244: 紧凑卡/收起块的分辨率级标题同样在"N个"右侧提示方向
        orient = getattr(row, "norm_orientation", "") or ""
        if orient:
            segs.append((f"({orient})", "TEXT_WEAK", 9, False, None))
    else:
        segs = [(row.label, "TEXT_SUB", 11, False, None),
                (f"{row.count} 张", "TEXT_WEAK", 9, False, None)]
    return flow(segs, x0)


def _pack_font(size: int, demi: bool = False) -> QFont:
    f = QFont()
    f.setPointSize(size)
    if demi:
        f.setWeight(QFont.DemiBold)
    return f


def _pack_segs_width(segs) -> int:
    """一段折行的总宽度(含段间距), 供右对齐时算起点 x。"""
    w = 0
    for text, ck, size, demi, lv in segs:
        w += QFontMetrics(_pack_font(size, demi)).horizontalAdvance(text) + 6
    return max(0, w - 6) if segs else 0


def _pack_draw_segs(painter, segs, x: int, y: int):
    for text, ck, size, demi, lv in segs:
        f = _pack_font(size, demi)
        tw = QFontMetrics(f).horizontalAdvance(text)
        if lv is not None:
            color = (QColor(T.DANGER) if lv == "fatal"
                     else QColor(T.WARN) if lv == "warn"
                     else QColor(T.TEXT_WEAK))
        else:
            color = QColor(getattr(T, ck)) if ck else QColor(T.TEXT)
        painter.setFont(f)
        painter.setPen(color)
        painter.drawText(QRect(x, y, tw + 4, PACK_LINE_H),
                         Qt.AlignLeft | Qt.AlignVCenter, text)
        x += tw + 6


def pack_header_h(row: HeaderRow, width: int) -> int:
    """紧凑组头高度(按折行行数); 单行时与普通组标题同高。"""
    n = len(pack_lines(row, width))
    return max(HEADER_H, PACK_PAD_TOP + n * PACK_LINE_H + PACK_PAD_BOTTOM)


def hit_test_pack(head_rect: QRect, hdr: HeaderRow, pos) -> str:
    """紧凑组头的命中: 三角/整块头部 = 折叠, 勾选框 = 全选本组。"""
    lay = pack_layout(head_rect, hdr)
    if lay["arrow"].adjusted(-6, -8, 6, 8).contains(pos):
        return "collapse"
    if lay["check"].adjusted(-4, -6, 4, 6).contains(pos):
        return "groupcheck"
    return "collapse"


def draw_pack_head(painter: QPainter, head_rect: QRect, hdr):
    """紧凑卡头块绘制(三角 + 全选勾选框 + 折行规格文字)。

    v230 从 draw_pack 拆出: 统一页最深层级的组标题(窄标题块)与紧凑卡头
    共用同一份绘制, 保证 "30fps"(多文件组窄标题块) 与 "50fps"(单文件紧凑卡)
    的标题完全同款同高。merge 首行"编码贴右缘"的口径不变。
    """
    painter.fillRect(head_rect, QBrush(QColor(T.HEADER_BG)))

    lay = pack_layout(head_rect, hdr)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor(T.TEXT_WEAK if hdr.level else T.TEXT_SUB)))
    ax, ay = lay["arrow"].x(), lay["arrow"].y()
    tri = QPainterPath()
    tri.moveTo(ax + 4, ay + 5)
    tri.lineTo(ax + 12, ay + 5)
    tri.lineTo(ax + 8, ay + 12)
    tri.closeSubpath()
    painter.drawPath(tri)
    draw_check_box(painter, lay["check"], getattr(hdr, "group_checked", 0))

    tx = lay["check"].x() + CHECK_BOX + PACK_TEXT_GAP
    # v228: 续行起点 = 卡片左缘 + PACK_CONT_X(PACK_CONT_X 是卡内相对坐标,
    # 绘制必须加上卡片自己的 x 偏移, 否则第二张卡的规格行会画到第一张卡上)
    cont_x = head_rect.x() + PACK_CONT_X
    lines = pack_lines(hdr, head_rect.width())
    y = lay["text_y0"]
    right = head_rect.x() + head_rect.width() - PACK_X
    for li, line in enumerate(lines):
        if hdr.merge_tag and li == 0 and len(line) > 1:
            # v227: 首行组名靠左, 只有编码格式贴右缘(与组名同一行)
            _pack_draw_segs(painter, line[:1], tx, y)
            rest = line[1:]
            _pack_draw_segs(painter, rest, right - _pack_segs_width(rest), y)
        else:
            _pack_draw_segs(painter, line,
                            tx if li == 0 else cont_x, y)   # 续行顶到勾选框下方
        y += PACK_LINE_H


def draw_pack(painter: QPainter, option, row: PackRow, selected: bool,
              hovered: bool, show_index: bool, ghost: bool, D=None,
              sel_paths=None):
    """组块(v226 紧凑卡 → v231 推广): 组头横贯块宽画在上半部, 本组的视频
    作为子卡横排在组头正下方(1 个文件=一格紧凑卡, 与 v226 完全一致)。

    v228: 统一头区高度 —— 同排块折行数不同时头高取最大、文本顶部锚定,
    首行"组名"与下半照片都保持水平。规格段续行顶到勾选框下方(最左)。
    收起时整卡只画头块, 不占整行(仅合并态有收起)。

    sel_paths: 勾选路径集合(model.grid_checked 镜像) —— 多文件块内子卡的
    逐张高亮按它画(条目级 Qt 选中表达不了"块内只勾一部分"); 单文件卡
    维持条目级 selected 不变。
    """
    hdr = row.header
    rect = option.rect
    cell_h = D.cell_h if D is not None else CELL_H
    uni = D._pack_uni_h() if D is not None else 0
    merge = bool(hdr.merge_tag)
    subs = row.all_rows()
    multi = len(subs) > 1
    if D is not None:
        geo = D._pack_geo(row)
        cols, nrows, sp = geo["cols"], geo["nrows"], geo["gap"]
        pitch = D._pack_pitch()
        cell_w = D.cell_w
    else:
        cols, nrows, sp, pitch, cell_w = 1, 1, 2, CELL_W + 2, CELL_W
    hh = pack_header_h(hdr, rect.width())
    head_h = max(hh, uni)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    if merge and hdr.collapsed:
        head_rect = QRect(rect)              # 收起: 整卡都是头, 没有照片
        photo_area = None
    else:
        head_rect = QRect(rect.x(), rect.y(), rect.width(), head_h)
        photo_area = True
    draw_pack_head(painter, head_rect, hdr)   # v230: 头块绘制拆出共用

    if photo_area is not None:
        vp_pos = D._vp_mouse() if D is not None else None
        for i, sub in enumerate(subs):
            col = i % cols
            rw = i // cols
            sr = QRect(rect.x() + col * pitch,
                       rect.y() + head_h + rw * (cell_h + sp), cell_w, cell_h)
            if multi:
                # v231: 块内逐张勾选/悬浮 —— 勾选看镜像集合, 悬浮看子卡矩形
                sel_i = (sub.photo.path in sel_paths
                         if sel_paths is not None else selected)
                hov_i = bool(hovered) and (vp_pos is None or sr.contains(vp_pos))
            else:
                sel_i, hov_i = selected, hovered
            opt = QStyleOptionViewItem(option)
            opt.rect = sr
            draw_photo(painter, opt, sub, sel_i, hov_i, show_index,
                       ghost and not multi, D)
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
    merge = bool(getattr(D, "merge_mode", False))
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    if ghost:
        painter.setOpacity(0.25)

    outer = rect.adjusted(2, 2, -2, -2)
    base = QPainterPath()
    base.addRoundedRect(QRectF(outer), 10, 10)
    if selected:
        # v220: 选中 = 深一档的浅蓝(资源管理器口径), 和悬浮的浅蓝区分开
        painter.fillPath(base, QBrush(QColor(T.SEL_SOFT)))
    elif hovered:
        painter.fillPath(base, QBrush(QColor(T.HOVER_SOFT)))

    if horizontal:
        n = max(14, cell_h - 8)
        icon_rect = QRect(outer.x() + 4, outer.y() + (cell_h - n) // 2, n, n)
    else:
        # 缩略图在单元格内水平居中: 避免小/中图标模式下图标靠左、右侧空一大块
        icon_rect = QRect(outer.x() + (outer.width() - thumb_h) // 2,
                          outer.y() + pad, thumb_h, thumb_h)
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

    # 叠加图标(勾选框 / 序号徽标 / 状态点)统一按缩略图大小等比缩放:
    # 原来固定 20px 勾选框 + 28x20 徽标 + 6px 内缩, 在小图标(缩略图约 46px)、
    # 中图标(约 66px)上等于糊住大半张图, 两角还往中间挤 —— 越小越看不清缩略图。
    side = min(icon_rect.width(), icon_rect.height())
    margin = max(3, min(6, round(side * 0.05)))
    box_s = max(12, min(20, round(side * 0.22)))

    if not horizontal:
        # 勾选框: 选中=蓝底白勾, 未选=白底细边。
        # 合并模式表示"已加入合并队列", 排序模式就是普通选中 —— 两种形态统一画法。
        box = QRect(icon_rect.x() + margin, icon_rect.y() + margin, box_s, box_s)
        radius = max(3.0, box_s * 0.25)
        bpath = QPainterPath()
        bpath.addRoundedRect(QRectF(box), radius, radius)
        if selected:
            painter.setPen(QPen(QColor(T.ACCENT), 1.4))
            painter.setBrush(QBrush(QColor(T.ACCENT)))
        else:
            painter.setPen(QPen(QColor("#B9C2CE"), 1.4))
            painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
        painter.drawPath(bpath)
        if selected:
            k = box_s / 20.0          # 对勾按 20px 基准等比缩放
            tick = QPen(QColor("#FFFFFF"), max(1.3, 2.1 * k))
            tick.setCapStyle(Qt.RoundCap)
            tick.setJoinStyle(Qt.RoundJoin)
            painter.setPen(tick)
            painter.setBrush(Qt.NoBrush)
            tp = QPainterPath()
            tp.moveTo(box.x() + 4.6 * k, box.center().y() + 0.4 * k)
            tp.lineTo(box.x() + 8.2 * k, box.bottom() - 5.2 * k)
            tp.lineTo(box.right() - 4.2 * k, box.y() + 5.4 * k)
            painter.drawPath(tp)

    if show_index:
        if horizontal:
            bh = max(13, min(16, icon_rect.height() - 6))
            bf = QFont()
            bf.setPixelSize(max(9, bh - 5))
            bf.setWeight(QFont.DemiBold)
            bw = min(icon_rect.width() - 4,
                     max(bh, QFontMetrics(bf).horizontalAdvance(str(row.index)) + 8))
            badge = QRect(icon_rect.x() + 3, icon_rect.y() + 3, bw, bh)
        else:
            # 左上角让给勾选框, 序号(先后顺序)统一挪到缩略图右下角, 尺寸跟着缩略图缩
            bh = max(13, min(20, round(side * 0.21)))
            bf = QFont()
            bf.setPixelSize(max(9, min(11, round(bh * 0.62))))
            bf.setWeight(QFont.DemiBold)
            padx = max(3, round(bh * 0.25))
            bw = max(bh, QFontMetrics(bf).horizontalAdvance(str(row.index)) + padx * 2)
            badge = QRect(icon_rect.right() - margin - bw + 1,
                          icon_rect.bottom() - margin - bh + 1, bw, bh)
        bp = QPainterPath()
        brad = max(4.0, badge.height() * 0.3)
        bp.addRoundedRect(QRectF(badge), brad, brad)
        painter.fillPath(bp, QBrush(QColor(0, 0, 0, 132)))
        painter.setFont(bf)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(badge, Qt.AlignCenter, str(row.index))

    if photo.status in (STATUS_PENDING, STATUS_DONE, STATUS_ERROR):
        color = {STATUS_PENDING: T.WARN, STATUS_DONE: T.OK, STATUS_ERROR: T.DANGER}[photo.status]
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(color)))
        dot = max(7, min(10, round(side * 0.12)))
        painter.drawEllipse(icon_rect.right() - margin - dot + 1, icon_rect.y() + margin,
                            dot, dot)

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
            f2 = QFont()
            f2.setPointSize(8)
            f2.setFamily(T.MONO_FAMILY)
            painter.setFont(f2)
            if merge:
                line = getattr(photo, "merge_line", "") or ""
                painter.setPen(QColor(T.DANGER if line.startswith("读取失败") else T.TEXT_WEAK))
                painter.drawText(QRect(tx, outer.y() + 4 + 16, tw, 14),
                                 Qt.AlignLeft | Qt.AlignVCenter,
                                 _elide(line, tw, f2))
            else:
                planned = bool(photo.planned)
                stamp = photo.synthetic_display(F.TAKEN)
                painter.setPen(QColor(T.ACCENT if planned else T.TEXT_WEAK))
                painter.drawText(QRect(tx, outer.y() + 4 + 16, tw, 14),
                                 Qt.AlignLeft | Qt.AlignVCenter, stamp)
    else:
        # 文字区占满整格宽度(而非缩略图宽度): 小/中图标下文件名与时间也能尽量显示完整
        info_w = cell_w - 2 * pad
        info = QRect(outer.x() + pad, icon_rect.bottom() + 4, info_w, info_h - 4)
        painter.setFont(f)
        painter.setPen(QColor(T.TEXT))
        painter.drawText(QRect(info.x(), info.y(), info.width(), 14),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         _elide(photo.name, info.width(), f))
        if show_info:
            f2 = QFont()
            f2.setPointSize(8)
            f2.setFamily(T.MONO_FAMILY)
            painter.setFont(f2)
            if merge:
                # 合并模式第二行: 规格 · 时长(读取失败则显示原因)
                line = getattr(photo, "merge_line", "") or ""
                painter.setPen(QColor(T.DANGER if line.startswith("读取失败") else T.TEXT_WEAK))
                painter.drawText(QRect(info.x(), info.y() + 14, info.width(), 13),
                                 Qt.AlignLeft | Qt.AlignVCenter,
                                 _elide(line, info.width(), f2))
            else:
                planned = bool(photo.planned)
                # 时间按档位逐级精简, 保证各档都能完整显示:
                # 超大/大图标=完整 年月日时分秒; 中/小图标=月-日 时:分(小图标也带月份)
                fmt = ("%Y-%m-%d %H:%M:%S" if cell_w >= 132
                       else "%m-%d %H:%M")
                dt = photo.taken_dt()
                stamp = dt.strftime(fmt) if dt else "—"
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
        # 合并模式: 格子左上角画勾选框, 第二行改显示规格/时长
        self.merge_mode = False

    def set_cell(self, w: int, h: int, horizontal: bool = False, show_info: bool = True):
        """更新网格尺寸并派生缩略图/信息区高度。

        给"文件名 + 时间"两行文字预留固定高度 INFO_MIN: 当单元格高度不足以同时放下
        方形缩略图与两行文字时, 优先压缩缩略图, 保证时间副标题(修改时间)始终在单元格内、
        不被下一行卡片或相邻年份标题遮挡(开启"年"分类后尤为明显)。
        """
        self.cell_w = w
        self.cell_h = h
        self.horizontal = horizontal
        self.show_info = show_info
        pad = PAD
        INFO_MIN = 34 if show_info else 0          # 文件名(14) + 时间(13) + 间距
        avail = h - pad * 2                        # 缩略图 + 信息区 的总可用高度
        thumb = w - pad * 2                        # 期望的方形缩略图边长
        if thumb + INFO_MIN <= avail:
            info = avail - thumb
        else:
            info = INFO_MIN
            thumb = max(16, avail - INFO_MIN)
        self.thumb_h = max(16, thumb)
        self.info_h = max(8, info)

    def _pack_gap(self) -> int:
        """相邻两个条目的视觉间隙: QListView 的 spacing 加在条目**两侧**各一份
        (实测 advance = sizeHint 宽 + 2*spacing), 所以间隙 = 2*spacing。"""
        view = self.parent()
        sp = max(0, view.spacing()) if view is not None and hasattr(view, "spacing") else 2
        return 2 * sp

    def _pack_pitch(self) -> int:
        """网格横向步距 = 卡宽 + 视觉间隙(块内子卡与普通卡列对齐用)。"""
        return self.cell_w + self._pack_gap()

    def _pack_cols(self, n: int) -> int:
        """多文件组块一行放几张子卡: 视口放得下就一排摆完, 放不下取视口列数。"""
        if n <= 1:
            return 1
        view = self.parent()
        gap = self._pack_gap()
        pitch = self.cell_w + gap
        vp = 600
        if view is not None and hasattr(view, "viewport"):
            vp = max(60, view.viewport().width() - 2)
        cols = (vp + gap) // pitch
        return max(1, min(n, int(cols)))

    def _pack_geo(self, row: PackRow) -> dict:
        """组块几何(v231): 列数 / 条目宽 / 子卡行数 —— sizeHint 与 draw_pack
        共用同一份, 保证画出来的子卡位置和布局占位一致。

        块宽 = cols × 列距 − 间隙: 块的 advance(宽+间隙) 正好等于 cols × 列距,
        块后面的条目与网格第 cols 列严丝合缝对齐。
        """
        subs = row.all_rows()
        n = len(subs)
        cols = self._pack_cols(n)
        gap = self._pack_gap()
        pitch = self.cell_w + gap
        w = cols * pitch - gap
        nrows = max(1, -(-n // cols))          # ceil(n / cols)
        return {"cols": cols, "nrows": nrows, "w": w, "gap": gap, "n": n}

    def _vp_mouse(self):
        """鼠标当前在视口里的位置(块内子卡的逐张悬浮高亮用), 拿不到返回 None。"""
        view = self.parent()
        if view is None or not hasattr(view, "viewport"):
            return None
        return view.viewport().mapFromGlobal(QCursor.pos())

    def _pack_uni_h(self) -> int:
        """组块统一头区高度 = 所有组块头高(按各自块宽折行)的最大值
        (v227 合并页引入, v228 推广到排序/统一/合并三态)。

        同排块的照片要水平 → 头区取最大、文本顶部锚定, 矮的块底部留白;
        收起卡(仅合并态)也用这个高度(只画头块), 头文字与展开卡的头文字同高。
        """
        view = self.parent()
        m = view.model() if view is not None else None
        rows = getattr(m, "rows", None)
        if rows is None:
            return 0
        vw = view.viewport().width() if view is not None and hasattr(view, "viewport") else 600
        c = getattr(self, "_uni_cache", None)
        if c is not None and c[0] == self.cell_w and c[1] == vw and c[2] is rows:
            return c[3]
        h = 0
        for r in rows:
            if isinstance(r, PackRow):
                w = self._pack_geo(r)["w"]
                h = max(h, pack_header_h(r.header, w))
            elif isinstance(r, CollapsedPack):
                # v232: 收起叶级组块只有头块, 按一格宽折行
                h = max(h, pack_header_h(r.header, self.cell_w))
        self._uni_cache = (self.cell_w, vw, rows, h)
        return h

    # v227 旧名保留一版: 历史验收脚本/外部调试还在调
    _merge_pack_uni_h = _pack_uni_h

    def sizeHint(self, option, index) -> QSize:
        from ..core.photo import PhotoModel
        row = index.data(PhotoModel.ROLE_ROW)
        if isinstance(row, HeaderRow):
            width = 600
            view = self.parent()
            if view is not None and hasattr(view, "viewport"):
                width = max(320, view.viewport().width() - 2)
            return QSize(width, HEADER_H)
        if isinstance(row, RowBreak):
            # v227: 隐形换行条 —— 占满一整排, 让后面的紧凑卡另起一行
            width = 600
            view = self.parent()
            if view is not None and hasattr(view, "viewport"):
                width = max(320, view.viewport().width() - 2)
            return QSize(width, 4)
        if isinstance(row, CollapsedPack):
            # v232: 收起的叶级组块 = 只有头块, 宽一格、不占整行(位置不跳)
            head_h = max(pack_header_h(row.header, self.cell_w),
                         self._pack_uni_h())
            return QSize(self.cell_w, head_h)
        if isinstance(row, PackRow):
            subs = row.all_rows()
            if len(subs) > 1:
                # v231: 多文件组块 —— 宽 = 子卡列数 × 卡宽, 高 = 统一头高 +
                # 子卡行数 × 卡高(视口放不下一排时块内自动折行)
                geo = self._pack_geo(row)
                head_h = max(pack_header_h(row.header, geo["w"]),
                             self._pack_uni_h())
                h = head_h + geo["nrows"] * self.cell_h + (geo["nrows"] - 1) * geo["gap"]
                return QSize(geo["w"], h)
            hh = pack_header_h(row.header, self.cell_w)
            # v228: 三态统一头高(同排紧凑卡照片水平); 收起只画头块(仅合并态)
            head_h = max(hh, self._pack_uni_h())
            if row.header.merge_tag and row.header.collapsed:
                return QSize(self.cell_w, head_h)
            return QSize(self.cell_w, head_h + self.cell_h)
        return QSize(self.cell_w, self.cell_h)

    def paint(self, painter, option, index):
        from ..core.photo import PhotoModel
        row = index.data(PhotoModel.ROLE_ROW)
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        view = self.parent()
        dragging = getattr(view, "live_ids", None)
        if isinstance(row, HeaderRow):
            draw_header(painter, option, row)
        elif isinstance(row, CollapsedPack):
            # v232: 收起的叶级组块 —— 整格只画头块(与展开组头同款), 点它可展开
            draw_pack_head(painter, option.rect, row.header)
        elif isinstance(row, PackRow):
            # 正在被拖动的紧凑卡: 原位画半透明(与照片卡一致)
            ghost = bool(dragging) and id(row.photo) in dragging
            draw_pack(painter, option, row,
                      bool(option.state & SELECTED),
                      bool(option.state & MOUSE_OVER) or self.hover_row == index.row(),
                      self.show_index, ghost, self,
                      sel_paths=getattr(index.model(), "grid_checked", None))
        elif isinstance(row, RowBreak):
            # v229: separator 换行条 = 合并页「无损/独立」分界, 画 ACCENT 蓝
            # 横线(与顶层组标题上缘的 3px 分隔线同款); 其余保持隐形。
            if getattr(row, "separator", False):
                r = option.rect
                painter.fillRect(QRect(r.x(), r.y(), r.width(), 3),
                                 QBrush(QColor(T.ACCENT)))
        else:
            # 正在被拖动的卡片: 在列表原位画成半透明, 视觉上告诉用户"这里会空出来"
            ghost = bool(dragging) and id(row.photo) in dragging
            draw_photo(painter, option, row,
                       bool(option.state & SELECTED),
                       bool(option.state & MOUSE_OVER) or self.hover_row == index.row(),
                       self.show_index, ghost, self)
        painter.restore()
