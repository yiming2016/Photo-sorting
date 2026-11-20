# -*- coding: utf-8 -*-
"""剪辑窗口右栏: 轨道片段清单(按轨道上的先后顺序排)。

为什么要这一栏: 时间线横向滚起来, 片段一多就"看不清一共有哪几段、谁在谁后面"。
这里按轨道顺序竖着排一遍, 一个片段一张卡片 —— 一眼看全, 还能拖着改顺序。

用户定的口径(v140):
  · 卡片可以拖动 → 真的改轨道上的先后顺序(后面的依次顺移), 不是只改列表;
  · 点卡片 → 在轨道上选中它; 裁剪线不在这一段上时顺带跳到它的起点,
    恰好在这一段里时单击只选中、双击才回起点(v159, 见主窗口 _on_clip_list_picked);
  · 只是"查看 + 调顺序", 不放属性(属性面板继续收着)。

⚠️ 拖动是自己接管的(v141), 不用 QListWidget 的 InternalMove:
   InternalMove 只在**松手**那一刻才给结果, 而用户要的是"拖的过程中轨道就跟着变"。
   所以: 按住就记下起始行 → 鼠标一动就算出要插到哪一行 → 当场把卡片挪过去,
   并把新顺序**实时**交给主窗口(主窗口只改轨道起点, 不重建这张表, 免得把卡片抽走);
   松手时再正式提交一次(重算无损/撤销点)。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

from . import theme as T
from .widgets import Marquee, OutsideClickWatcher, split_name_stem

CARD_H = 60              # 一张卡片的高度
THUMB = 44               # 左边缩略图边长
PAD = 8
AUTO_PX = 20             # 拖到离上下沿这么近就自动滚
DRAG_SLOP = 4            # 移动超过这几个像素才算"在拖"(否则算点击)
MQ_SLOP = 5              # 框选: 移动超过这几个像素就算"拉出了框"
LONG_PRESS_MS = 220      # 按住空白不动 → 进入框选模式


def _fmt_dur(sec: float) -> str:
    sec = max(0.0, float(sec or 0.0))
    if sec < 60:
        return f"{sec:.1f}s"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}:{s:04.1f}"


# v192: 卡片副标题那行右端的小铅笔(点一下就地改名), 画和命中都用它, 别各写一套。
# v193: 高度从 18 收到 12 —— 原来那个方块把副标题整行都撑满了, 看着像第二行标题。
def _pencil_rect(r: QRectF) -> QRectF:
    return QRectF(r.right() - 24, r.top() + 31, 18, 12)


def _draw_pencil(p: QPainter, rect: QRectF, color):
    """自绘小铅笔: 不用文字符号(雅黑缺字形)也不引外部图标资源。"""
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    s = min(rect.width(), rect.height()) / 20.0
    p.translate(rect.center().x() - 10 * s, rect.center().y() - 10 * s)
    p.scale(s, s)
    pen = QPen(QColor(color), 1.7)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    body = QPainterPath()
    body.moveTo(4.0, 15.0)
    body.lineTo(14.0, 5.0)
    body.lineTo(17.0, 8.0)
    body.lineTo(7.0, 18.0)
    body.closeSubpath()
    p.drawPath(body)
    tip = QPainterPath()                   # 笔尖
    tip.moveTo(2.2, 19.6)
    tip.lineTo(4.0, 15.0)
    tip.lineTo(7.0, 18.0)
    tip.lineTo(2.2, 19.6)
    p.drawPath(tip)
    sep = QPainterPath()                   # 笔头分界
    sep.moveTo(11.9, 7.1)
    sep.lineTo(14.9, 10.1)
    p.drawPath(sep)
    p.restore()


class _ClipNameEdit(QLineEdit):
    """卡片上就地改名的小输入框。

    v193 口径: **只有回车算确认**; 点输入框外面(包括空白处)或 Esc 一律放弃。
    以前挂 editingFinished, 焦点一丢就把半截名字提交了 —— 用户明确要求改掉。
    """

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, text: str, parent=None):
        super().__init__(text or "", parent)
        self._done = False
        self.setObjectName("clipNameEdit")
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setFrame(False)
        self.setStyleSheet(
            f"QLineEdit#clipNameEdit{{background:{T.SURFACE}; color:{T.TEXT};"
            f" border:1px solid {T.ACCENT}; border-radius:3px;"
            f" padding:0px 2px; font-size:12px;}}")
        # 点输入框外面就放弃（不依赖焦点策略, 见 OutsideClickWatcher）
        self._outside = OutsideClickWatcher(self)
        self._outside.outsideClicked.connect(self._cancel)

    def start_outside(self):
        self._outside.start()

    def stop_outside(self):
        self._outside.stop()

    def add_inside_widget(self, w):
        """v194: 后缀标签算输入框内部, 点它不放弃编辑。"""
        self._outside.add_inside(w)

    def keyPressEvent(self, event):
        if self._done:
            return
        key = event.key()
        if key == Qt.Key_Escape:
            self._cancel()
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._done = True
            self.committed.emit(self.text())
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        if not self._done:
            self._cancel()
        super().focusOutEvent(event)

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self.cancelled.emit()


class _ClipListView(QListWidget):
    """清单本体: 把鼠标按下/移动/松手接过来, 交给面板做"实时挪动"。

    ⚠️ 为什么要自己干: Qt 的 InternalMove 只在松手后发 rowsMoved, 拖动过程中
       列表里什么都没变 —— 看着就像"拖了半天没反应, 一松手才跳一下"。

    v180 追加: **按住 Ctrl 多选** + **长按空白处拉框选**(用户明确要求的功能)。
    """

    def __init__(self, panel: "ClipListPanel"):
        super().__init__(panel)
        self._panel = panel
        self.setMouseTracking(True)
        # v194: 鼠标压在改名图标上时指针变「小手」(v216 起整片都是小手, 见 app/ui/cursor.py)
        self.viewport().setMouseTracking(True)
        # 框选(v180)
        self._mq = None                 # Marquee 橡皮筋
        self._mq_anchor = None           # 按下位置(视口坐标)
        self._mq_live = False            # 已经拉出框了
        self._mq_add = False             # 按住 Ctrl = 追加
        self._mq_base: list = []         # 按下时已选中的
        self._lp_timer = QTimer(self)    # 长按空白 → 框选
        self._lp_timer.setSingleShot(True)
        self._lp_timer.timeout.connect(self._on_long_press)
        # v181: Ctrl+点卡片时, 松手不能再让 Qt 发 itemClicked ——
        # 那个信号会走"普通单击"分支, 把刚加进来的多选冲成只剩一个,
        # 还会顺手把裁剪线拽到这一段开头(用户报的"Ctrl 多选完全没用")。
        self._ctrl_press = False
        # v192: 按在卡片右端小铅笔上 = 就地改名, 这一下不能被当成拖动/单击
        self._icon_press = False

    # ---------------------------------------------------------------- 框选
    def _on_long_press(self):
        """按住空白不动 → 进入框选模式(v180): 立刻把框显示出来。

        没有这个反馈的话, 用户会以为"按住没用" —— 而实际上这时候拖动就是在拉框。
        """
        if self._mq_anchor is None or self._mq_live:
            return
        if self._mq is None:
            self._mq = Marquee(self.viewport())
        self._mq.setGeometry(QRect(self._mq_anchor, QSize(0, 0)))
        self._mq.show()

    def _mq_start(self, pos):
        """在空白处按下: 记下起点, 准备拉框。"""
        self._mq_anchor = pos
        self._mq_live = False
        self._mq_add = bool(self._panel._ctrl_held)
        self._mq_base = list(self._panel._selected_clips())

    def _mq_update(self, pos):
        """拖动中: 更新框, 并把框住的卡片选上。"""
        if self._mq_anchor is None:
            return
        if not self._mq_live:
            if (pos - self._mq_anchor).manhattanLength() < MQ_SLOP:
                return
            self._mq_live = True
        if self._mq is None:
            self._mq = Marquee(self.viewport())
        rect = QRect(self._mq_anchor, pos).normalized()
        self._mq.setGeometry(rect)
        self._mq.show()
        self._panel._select_in_rect(rect, self._mq_base if self._mq_add else None,
                                    self._mq_add)

    def _mq_end(self, pos):
        """松手: 收框。

        v181: **点一下空白 = 取消多选**(用户要求), 并且要通知轨道一起清掉;
        按住 Ctrl 点空白才是"保留原选中"(追加框选用)。
        """
        if self._mq is not None:
            self._mq.hide()
            self._mq.setGeometry(QRect())
        self._mq_anchor = None
        if not self._mq_live:
            if self._mq_add:
                self._panel._restore_selection(self._mq_base)
            else:
                self._panel.clear_selection()
        else:
            # 拉出框了: 这才是真的"选了这些" → 通知外部同步到轨道
            self._panel._emit_panel_selection()
        self._mq_live = False
        self._mq_base = []

    # ---------------------------------------------------------------- 事件
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._ctrl_press = False
            pos = event.position().toPoint()
            it = self.itemAt(pos)
            self._panel._ctrl_held = bool(event.modifiers() & Qt.ControlModifier)
            # v192: 小铅笔 = 就地改名, 不走拖动/点选那一套
            if it is not None and self._panel._icon_hit(pos, self.row(it)):
                self._icon_press = True
                self._panel.begin_rename(self.row(it))
                event.accept()
                return
            if it is None:
                # 按在空白处(卡片之间的空隙/列表末尾): 长按不动 → 框选
                self._lp_timer.start(LONG_PRESS_MS)
                self._mq_start(pos)
                event.accept()
                return
            # 按在卡片上: Ctrl = 多选, 否则是"点一下定位"
            if self._panel._ctrl_held:
                self._panel._toggle_clip(self.row(it))
                self._ctrl_press = True          # 松手时吞掉, 别让 Qt 再当普通点击
                event.accept()
                return
        super().mousePressEvent(event)          # 先让 Qt 把选中处理好
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            it = self.itemAt(pos)
            if it is not None:
                self._panel._drag_start(self.row(it), pos.y())

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._mq_anchor is None and self._panel._drag is None:
            self._panel._set_icon_hover(pos)
        if self._mq_anchor is not None:
            self._mq_update(pos)
            event.accept()
            return
        if self._panel._drag_move(pos.y()):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._icon_press:
            # 点的是小铅笔: 别让 Qt 补发 itemClicked(会把裁剪线拽走)
            self._icon_press = False
            event.accept()
            return
        if self._mq_anchor is not None:
            self._lp_timer.stop()
            self._mq_end(event.position().toPoint())
            event.accept()
            return
        if self._ctrl_press:
            # Ctrl 点在 press 里已经处理完了。走 super() 的话 Qt 会补发
            # itemClicked → 面板按"普通单击"把多选冲掉, 还跳裁剪线。
            self._ctrl_press = False
            event.accept()
            return
        if self._panel._drag_end():
            # 拖过就别再当"点击"处理: 否则松手会顺手把裁剪线拽到这一段开头
            self._panel._swallow_click = True
            try:
                super().mouseReleaseEvent(event)
            finally:
                self._panel._swallow_click = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        """右键: 卡片上 = 单段菜单; 空白处(框选后常落在空白) = 整组菜单。

        v181: 框选完右键不能"什么都不弹" —— 主窗口会按面板的选区把「对比」
        放出来。多选状态由面板自己记着, 主窗口问 `selected_clips()` 即可,
        信号仍保持 (clip, gpos) 两个参数(旧接线/测试都兼容)。
        """
        it = self.itemAt(event.pos())
        if it is not None:
            self._panel._request_menu(self.row(it), event.globalPos())
        elif self._panel._selected_clips():
            self._panel.menuRequested.emit(None, event.globalPos())
        else:
            return
        event.accept()



class _CardDelegate(QStyledItemDelegate):
    """一张卡片: 序号 + 缩略图 + 文件名 + 时长/轨号。

    ⚠️ 不用 QListWidget::item 的 QSS 上色: 主题里那条规则会吞掉自定义背景,
       选中态就看不出来了 —— 全部自己画。
    """

    def __init__(self, panel: "ClipListPanel"):
        super().__init__(panel)
        self._panel = panel

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), CARD_H)

    def paint(self, p: QPainter, option, index):
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        # 正被拖着的那张画淡一点: 让人看出"手里拎着的是这一张"
        if self._panel.is_dragging() and index.row() == self._panel.drag_row():
            p.setOpacity(0.45)
        r = QRectF(option.rect).adjusted(2, 2, -2, -2)
        selected = (bool(option.state & QStyle.State_Selected)
                    or self._panel._is_highlighted(index))
        hover = bool(option.state & QStyle.State_MouseOver)

        # ---- 卡片底 ----
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
        if selected:
            p.fillPath(path, QColor(T.ACCENT_SOFT))
            p.setPen(QPen(QColor(T.ACCENT), 1.6))
        else:
            p.fillPath(path, QColor(T.SURFACE if hover else T.SURFACE_ALT))
            p.setPen(QPen(QColor(T.BORDER), 1.0))
        p.drawPath(path)

        # ---- 序号 ----
        idx = index.row() + 1
        num = str(idx) if idx < 100 else "99+"
        nf = QFont(option.font)
        nf.setPointSizeF(max(7.5, nf.pointSizeF() - 1.0))
        nf.setBold(True)
        p.setFont(nf)
        p.setPen(QColor(T.ACCENT if selected else T.TEXT_WEAK))
        p.drawText(QRectF(r.left() + 4, r.top(), 16, r.height()),
                   Qt.AlignCenter, num)

        # ---- 缩略图 ----
        tr = QRectF(r.left() + 22, r.top() + (r.height() - THUMB) / 2, THUMB, THUMB)
        pm = self._panel.thumb_of(index)
        tp = QPainterPath()
        tp.addRoundedRect(tr, 5, 5)
        p.save()
        p.setClipPath(tp)
        if pm is not None and not pm.isNull():
            # 等比铺满 + 居中裁切(不要拉伸变形)
            sc = max(tr.width() / max(1, pm.width()), tr.height() / max(1, pm.height()))
            w, h = pm.width() * sc, pm.height() * sc
            dst = QRectF(tr.center().x() - w / 2, tr.center().y() - h / 2, w, h)
            p.drawPixmap(dst, pm, QRectF(pm.rect()))
        else:
            p.fillRect(tr, QColor("#E8ECF2"))
            p.setPen(QColor(T.TEXT_WEAK))
            f2 = QFont(option.font)
            f2.setPointSizeF(max(7.0, f2.pointSizeF() - 2.0))
            p.setFont(f2)
            p.drawText(tr, Qt.AlignCenter, self._panel.kind_of(index))
        p.restore()
        p.setPen(QPen(QColor(T.BORDER), 1.0))
        p.drawPath(tp)

        # ---- 文字 ----
        x0 = tr.right() + 8
        w = r.right() - x0 - 6
        if w > 20:
            name = self._panel.name_of(index)
            bf = QFont(option.font)
            bf.setBold(True)
            p.setFont(bf)
            p.setPen(QColor(T.TEXT))
            fm = QFontMetrics(bf)
            nm = fm.elidedText(name, Qt.ElideMiddle, int(w))
            p.drawText(QRectF(x0, r.top() + 12, w, 17), Qt.AlignLeft | Qt.AlignVCenter, nm)

            sub = self._panel.sub_of(index)
            sf = QFont(option.font)
            sf.setPointSizeF(max(7.5, sf.pointSizeF() - 1.0))
            p.setFont(sf)
            p.setPen(QColor(T.TEXT_WEAK))
            # 右边 26px 留给小铅笔, 别让「第 1 轨」这行字压到图标上
            p.drawText(QRectF(x0, r.top() + 30, max(10, w - 26), 15),
                       Qt.AlignLeft | Qt.AlignVCenter, sub)
        # v192: 「0.0s · 第 1 轨」这行居右的小铅笔 —— 点一下就在卡片上改名
        hot = (selected or hover
               or self._panel._hover_icon_row == index.row())
        _draw_pencil(p, _pencil_rect(r), T.ACCENT if hot else T.TEXT_WEAK)
        p.restore()


class ClipListPanel(QWidget):
    """右栏: 片段清单(可拖动改顺序 / 点一下定位)。"""

    clipPicked = Signal(object, bool)   # 点了某张卡片: (clip, 是否强制回起点) v159
    menuRequested = Signal(object, object)   # 右键卡片: (clip, 全局坐标)
    orderPreview = Signal(list)      # 拖动**过程中**: 顺序变了, 主窗口实时改轨道
    orderChanged = Signal(list)      # 松手: 新顺序定下来, 主窗口正式提交
    selChanged = Signal(list)        # v180: 右栏里多选了这些片段 → 主窗口同步到轨道
    renameCommitted = Signal(object)  # v192: 卡片上就地改完名(Clip)

    ROLE_ID = Qt.UserRole + 1
    ROLE_NAME = Qt.UserRole + 2
    ROLE_SUB = Qt.UserRole + 3
    ROLE_THUMB = Qt.UserRole + 4
    ROLE_KIND = Qt.UserRole + 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clips: list = []
        self._syncing = False
        self._drag = None            # 正在拖: {"row","y0","moved"}
        self._swallow_click = False  # 刚拖完: 别把松手当成"点了这张卡"
        self._hi: set = set()        # 轨道上被多选的片段(卡片一起亮起来)
        # v180: 右栏自己也能多选(Ctrl+点 / 长按空白拉框)
        self._sel: list = []         # 面板里选中的片段(按行序)
        self._ctrl_held = False      # 上一次按下时有没有按 Ctrl
        self._echo = False           # 选中变化的回灌保护(见 _emit_panel_selection)
        # v192: 卡片右端小铅笔 / 就地改名输入框
        self._hover_icon_row = -1
        self._rename_edit = None
        self._rename_suffix = None    # v194: 输入框右边那个只读后缀标签
        self.setMinimumWidth(232)
        self.setMaximumWidth(340)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # v164: 标题挪进白框里 —— 以前「轨道片段」单占框外一行, 那 20px 高度白花
        # 了(用户: 页面空间宝贵)。现在框从顶上就是白的, 标题写在框内第一行。
        self.box = QFrame()
        self.box.setObjectName("clipListBox")
        bx = QVBoxLayout(self.box)
        bx.setContentsMargins(0, 0, 0, 0)
        bx.setSpacing(0)

        self.title = QLabel("轨道片段")
        self.title.setObjectName("clipListTitle")
        bx.addWidget(self.title)

        self.list = _ClipListView(self)
        self.list.setObjectName("clipList")
        # 拖放自己接管(v141), 关掉 Qt 自带的 InternalMove, 免得两套一起跑
        self.list.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(Qt.NoFocus)      # 空格归播放, 别被列表吃掉
        self.list.setMouseTracking(True)
        self.list.setItemDelegate(_CardDelegate(self))
        # 白底和描边已经在外壳上, 列表自己透明(不然会出现"框里套框")
        self.list.setStyleSheet(
            f"QListWidget#clipList{{background:transparent; border:none;"
            f" padding:0px 4px 4px 4px;}}"
            f"QListWidget#clipList::item{{border:none; background:transparent;}}")
        self.list.itemClicked.connect(self._on_clicked)
        self.list.itemDoubleClicked.connect(self._on_double_clicked)
        # ⚠️ 不要再连 itemActivated: Windows 上双击会先发 clicked 再发 activated,
        #    两条都连就是"双击触发两次跳转"(v159 拆成单击/双击两套口径)。
        bx.addWidget(self.list, 1)

        self.empty = QLabel("轨道上还没有片段。\n把左边素材 ＋ 进来，或者直接拖到时间线上。")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setObjectName("clipListEmpty")
        bx.addWidget(self.empty, 1)
        root.addWidget(self.box, 1)

        self.box.setStyleSheet(
            f"QFrame#clipListBox{{background:{T.SURFACE};"
            f" border:1px solid {T.BORDER}; border-radius:8px;}}")
        self.title.setStyleSheet(
            f"QLabel#clipListTitle{{color:{T.TEXT_SUB}; font-size:12px;"
            f" font-weight:600; padding:5px 8px 3px 8px; background:transparent;}}")
        self.empty.setStyleSheet(
            f"QLabel#clipListEmpty{{color:{T.TEXT_WEAK}; font-size:12px;"
            f" padding:18px 6px; background:transparent;}}")
        self.empty.setVisible(True)
        self.list.setVisible(False)

    def sizeHint(self) -> QSize:
        """空着和"有片段"两种状态必须一样宽(v164)。

        布局按 sizeHint 给这栏分宽度: 空着时可见的是那句换行提示(量出来 236),
        一加片段就换成列表(256) —— 于是"加个片段, 右栏自己变宽、预览跟着缩"。
        这里固定按列表那份算, 两种状态就一样宽。
        """
        s = super().sizeHint()
        try:
            return QSize(max(s.width(), self.list.sizeHint().width() + 2), s.height())
        except Exception:
            return s

    # --------------------------------------------- 小铅笔 / 就地改名(v192)
    def _card_rect(self, row: int) -> QRectF:
        """卡片内容矩形(视口坐标, 与 delegate 的 adjusted(2,2,-2,-2) 一致)。"""
        it = self.list.item(row)
        if it is None:
            return QRectF()
        return QRectF(self.list.visualItemRect(it)).adjusted(2, 2, -2, -2)

    def _icon_hit(self, pos, row: int) -> bool:
        r = self._card_rect(row)
        return bool(r.isValid()
                    and _pencil_rect(r).contains(QPointF(pos)))

    def _set_icon_hover(self, pos):
        """鼠标压在小铅笔上时把图标点亮, 让用户看得出这里能点。

        v194: 指针同时换成「小手」; v216: 整个清单都是小手了(全局规则), 这里只管点亮。
        """
        it = self.list.itemAt(pos)
        hit = self.list.row(it) if (it is not None
                                    and self._icon_hit(pos, self.list.row(it))) else -1
        if hit != self._hover_icon_row:
            self._hover_icon_row = hit
            self.list.viewport().update()

    def begin_rename(self, row: int):
        """点小铅笔 = 在卡片名字那一行盖一个输入框, 原地改名(不弹窗)。

        v194: 输入框只放主名, 磁盘文件的后缀作为只读标签贴在右边 ——
        和对比窗口的重命名同一套「输入框 + .mp4」格式, 后缀改不到也删不掉。
        """
        it = self.list.item(row)
        clip = self._clip_at(row)
        if it is None or clip is None:
            return
        self._end_rename()
        r = self._card_rect(row)
        x0 = r.left() + 22 + THUMB + 8
        x1 = r.right() - 6
        stem, ext = split_name_stem(str(getattr(clip, "name", "") or ""),
                                    str(getattr(clip, "source", "") or ""))
        ed = _ClipNameEdit(stem, self.list.viewport())
        suffix_w = 0
        if ext:
            suffix_w = QFontMetrics(ed.font()).horizontalAdvance(ext) + 10
            lab = QLabel(ext, self.list.viewport())
            lab.setObjectName("clipNameSuffix")
            lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            lab.setStyleSheet(
                f"QLabel#clipNameSuffix{{background:{T.SURFACE}; color:{T.TEXT_SUB};"
                f" border:1px solid {T.ACCENT}; border-left:none;"
                f" padding:0px 3px; font-size:12px;}}")
            lab.setGeometry(int(x0 + max(60, x1 - x0 - suffix_w)),
                            int(r.top() + 8), int(suffix_w), 22)
            lab.show()
            self._rename_suffix = lab
            ed.add_inside_widget(lab)      # 点后缀标签不算「点输入框外面」
        ed.setGeometry(int(x0), int(r.top() + 8),
                       int(max(60, x1 - x0 - suffix_w)), 22)
        ed.selectAll()
        ed.show()
        ed.setFocus(Qt.MouseFocusReason)
        ed.committed.connect(lambda name, c=clip, e=ext:
                             self._finish_rename(c, name, e))
        ed.cancelled.connect(self._end_rename)
        self._rename_edit = ed
        ed.start_outside()             # v193: 点空白处 / Esc 放弃

    def _end_rename(self):
        ed = self._rename_edit
        self._rename_edit = None
        lab = getattr(self, "_rename_suffix", None)
        self._rename_suffix = None
        if lab is not None:
            try:
                lab.hide()
                lab.deleteLater()
            except Exception:
                pass
        if ed is not None:
            try:
                ed.stop_outside()
                ed.blockSignals(True)
                ed.hide()
                ed.deleteLater()
            except Exception:
                pass

    def _finish_rename(self, clip, name: str, ext: str = ""):
        name = (name or "").strip()
        self._end_rename()
        old = str(getattr(clip, "name", "") or "")
        full = name + (ext or "")
        if not name or full == old:
            self.list.viewport().update()
            return
        clip.name = full
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it is not None and it.data(self.ROLE_ID) == id(clip):
                it.setData(self.ROLE_NAME, name)
                break
        self.list.viewport().update()
        self.renameCommitted.emit(clip)   # 主窗口: 轨道 / 属性 / 撤销点

    # ---------------------------------------------------------------- 数据
    def set_clips(self, clips, thumbs: dict | None = None):
        """重建清单。clips 必须是**已经按轨道顺序排好的**。"""
        thumbs = thumbs or {}
        self._clips = list(clips or [])
        self._hi = set()
        self._sel = []            # 重建了表, 旧选中作废(v180)
        self._syncing = True
        try:
            self.list.clear()
            for c in self._clips:
                # 显示名优先用片段自己的名字(v152: 右键改过名要能看见),
                # 没改过时 clip.name 本来就是文件名。
                name = (str(getattr(c, "name", "") or "")
                        or os.path.basename(str(getattr(c, "source", "")) or ""))
                sub = f"{_fmt_dur(getattr(c, 'duration', 0.0))} · 第 {int(c.track) + 1} 轨"
                if getattr(c, "detached", False):
                    sub += " · 音轨已分离"
                elif getattr(c, "muted", False):
                    sub += " · 静音"
                it = QListWidgetItem()
                it.setData(self.ROLE_ID, id(c))
                it.setData(self.ROLE_NAME, name or "(未知)")
                it.setData(self.ROLE_SUB, sub)
                it.setData(self.ROLE_THUMB, thumbs.get(getattr(c, "source", "")))
                _kind = getattr(c, "kind", "")
                it.setData(self.ROLE_KIND,
                           {"video": "视频", "audio": "音频"}.get(_kind, "图片"))
                it.setSizeHint(QSize(200, CARD_H))
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
                self.list.addItem(it)
        finally:
            self._syncing = False
        n = len(self._clips)
        self.title.setText(f"轨道片段 · {n} 个" if n else "轨道片段")
        self.list.setVisible(n > 0)
        self.empty.setVisible(n == 0)

    def select_clip(self, clip):
        """外部选中变化时, 让清单里对应的卡片也高亮(点卡片时不用回灌)。"""
        self._syncing = True
        try:
            hit = -1
            for i in range(self.list.count()):
                if self.list.item(i).data(self.ROLE_ID) == id(clip):
                    hit = i
                    break
            if hit >= 0:
                self.list.setCurrentRow(hit)
                self.list.scrollToItem(self.list.item(hit))
            else:
                self.list.clearSelection()
        finally:
            self._syncing = False

    # delegate 用: 按行取数据(比每张卡片各存一份 dict 省事)
    def _data(self, index, role, default=""):
        it = self.list.item(index.row()) if self.list.count() > index.row() else None
        if it is None:
            return default
        v = it.data(role)
        return default if v is None else v

    def thumb_of(self, index):
        v = self._data(index, self.ROLE_THUMB, None)
        return v if isinstance(v, QPixmap) else None

    def set_thumb(self, source: str, pm):
        """补一张缩略图(抽帧线程晚点回来时用, 不重建整张表)。

        ⚠️ key 用**源路径**: 同一个素材切出来的几段共用一张图。
        """
        if not source or pm is None or (isinstance(pm, QPixmap) and pm.isNull()):
            return
        hit = False
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it is None or it.data(self.ROLE_THUMB) is not None:
                continue
            c = self._clip_at(i)
            if c is not None and str(getattr(c, "source", "")) == source:
                it.setData(self.ROLE_THUMB, pm)
                hit = True
        if hit:
            self.list.viewport().update()

    def name_of(self, index) -> str:
        return str(self._data(index, self.ROLE_NAME, ""))

    def sub_of(self, index) -> str:
        return str(self._data(index, self.ROLE_SUB, ""))

    def kind_of(self, index) -> str:
        return str(self._data(index, self.ROLE_KIND, ""))

    def set_multi_highlight(self, clips):
        """轨道上同时选中了好几段(Ctrl+A 全选): 对应的卡片一起亮。

        ⚠️ 列表自己是单选(空格要留给播放, 多选还会和"拖动改顺序"打架),
        所以"亮起来"是画上去的, 不是真的 Qt 选中。
        """
        if self._echo:
            # 这是**我们自己**刚发出的选中变化被主窗口回灌回来 —— 别让它覆盖
            # 面板自己的状态(比如只选了一个时, 回灌的是 None, 会把选中清掉)。
            return
        self._apply_selection(clips)

    def _apply_selection(self, clips):
        """**唯一**改选中状态的地方(v180)。

        两条来源都要经过它, 否则两套状态会互相覆盖:
          · 轨道那边选中变了 → 主窗口调 set_multi_highlight();
          · 右栏自己 Ctrl+点/拉框 → 面板调 _apply_selection()。
        两边都落到 `_sel`(有序)+ `_hi`(画的 id 集), 画的时候只看 `_hi`。
        """
        clips = [c for c in (clips or []) if c is not None]
        self._sel = clips
        ids = {id(c) for c in clips}
        if ids != self._hi:
            self._hi = ids
            self.list.viewport().update()

    def _is_highlighted(self, index) -> bool:
        if not self._hi:
            return False
        return self._data(index, self.ROLE_ID, None) in self._hi

    # ---------------------------------------------------------------- 多选(v180)
    # 用户要的是**右栏「轨道片段」**这一块能多选: Ctrl+点 逐个加/减,
    # 长按空白处拉框 一次框一片。
    # ⚠️ 列表本身保持 SingleSelection(多选会和"拖动改顺序"打架, 而且空格要留给播放),
    #    所以选中状态自己记一份(_sel), 画的时候用 _is_highlighted 那套(见 delegate)。
    def _selected_clips(self) -> list:
        return list(self._sel)

    def selected_clips(self) -> list:
        """给主窗口问的公开口径: 右栏现在选中的是哪几段(按行序)。"""
        return list(self._sel)

    def clear_selection(self):
        """取消多选(v181): 面板 / Qt 选中 / 轨道三处一起清干净。"""
        self._apply_selection([])
        self.list.setCurrentRow(-1)
        self.list.clearSelection()
        self.list.viewport().update()
        self._emit_panel_selection()

    def _toggle_clip(self, row: int):
        """Ctrl+点卡片: 加一个 / 撤一个, 其余保持。"""
        clip = self._clip_at(row)
        if clip is None:
            return
        cur = list(self._sel)
        hit = next((i for i, c in enumerate(cur) if c is clip), -1)
        if hit >= 0:
            cur.pop(hit)
        else:
            cur.append(clip)
        self._apply_selection(cur)
        self._emit_panel_selection()

    def _select_in_rect(self, rect: QRect, base=None, add: bool = False):
        """框选: 把框住的卡片选上(base=None 就是替换, base 给了就是追加)。"""
        hits = []
        for row in range(self.list.count()):
            it = self.list.item(row)
            if it is None:
                continue
            r = self.list.visualItemRect(it)
            if not r.isValid():
                continue
            if rect.intersects(r):
                clip = self._clip_at(row)
                if clip is not None:
                    hits.append(clip)
        cur = list(base) if add else []
        for c in hits:
            if not any(c is x for x in cur):
                cur.append(c)
        # ⚠️ 边拉边改选中, 但**不通知外部**: 通知会触发轨道刷新(预览重绘),
        #    每挪一个像素都刷一次太重。等松手时统一通知一次(见 _mq_end)。
        self._apply_selection(cur)

    def _restore_selection(self, clips):
        """只是点了一下空白(没拉出框): 恢复原来的选中, 不清空。"""
        self._apply_selection(clips)

    def _emit_panel_selection(self):
        """面板选中变了 → 通知外部(主窗口同步到轨道上)。

        `_echo` 用来挡住"自己发出去 → 主窗口回灌回来"这一圈:
        回灌时主窗口会调 set_multi_highlight, 那会把面板自己的选中覆盖掉
        (比如只选了一个时回灌的是 None, 选中就被清空了)。
        """
        if self._syncing:
            return
        self._echo = True
        try:
            self.selChanged.emit(list(self._sel))
        finally:
            self._echo = False

    # ---------------------------------------------------------------- 交互
    def _clip_at(self, row: int):
        if 0 <= row < len(self._clips):
            return self._clips[row]
        return None

    def _on_clicked(self, item):
        # v181 二道保险: 万一 Qt 还是补发了 itemClicked, Ctrl 那一次点击
        # 绝不能走"普通单击"分支(会把多选冲成单个 + 跳裁剪线)。
        if self._syncing or self._swallow_click or self._ctrl_held:
            return
        row = self.list.row(item)
        clip = self._clip_at(row)
        if clip is not None:
            # 普通单击 = 只看这一段(选中集合换成它)
            self._apply_selection([clip])
            self.clipPicked.emit(clip, False)

    def _on_double_clicked(self, item):
        """双击卡片 = 一定要把裁剪线放回这一段开头(单击只负责选中)。"""
        if self._syncing or self._swallow_click:
            return
        row = self.list.row(item)
        clip = self._clip_at(row)
        if clip is not None:
            self.clipPicked.emit(clip, True)

    def _request_menu(self, row: int, gpos):
        """右键某张卡片 = 针对这一段的菜单(重命名 / 删除)。"""
        clip = self._clip_at(row)
        if clip is None:
            return
        self.list.setCurrentRow(row)         # 右键也顺手高亮这一段, 不跳裁剪线
        self.menuRequested.emit(clip, gpos)

    # ---------------------------------------------------------------- 拖动
    def is_dragging(self) -> bool:
        """主窗口刷新前要问一句: 正拖着的时候别重建这张表(会把卡片抽走)。"""
        return self._drag is not None

    def drag_row(self) -> int:
        return int(self._drag["row"]) if self._drag else -1

    def _drag_start(self, row: int, y: int):
        it = self.list.item(row)
        if it is None:
            return
        self._drag = {"row": row, "y0": y, "moved": False}
        self.list.setCurrentRow(row)
        self.list.viewport().update()

    def _drag_move(self, y: int) -> bool:
        d = self._drag
        if d is None:
            return False
        if not d["moved"]:
            if abs(y - d["y0"]) <= DRAG_SLOP:
                return True              # 手抖不算拖
            d["moved"] = True
        target = self._row_at(y)
        if target != d["row"]:
            self._move_item(d["row"], target)
            d["row"] = target
            self.orderPreview.emit(list(self._clips))
        else:
            self._auto_scroll(y)
        return True

    def _drag_end(self) -> bool:
        d = self._drag
        if d is None:
            return False
        self._drag = None
        self.list.viewport().update()
        if not d["moved"]:
            return False                 # 没真的挪动过 → 交给点击处理
        self.orderChanged.emit(list(self._clips))
        return True

    def _row_at(self, y: int) -> int:
        """鼠标挪到这个高度时, 卡片该落在第几行(按"越过中线就换位置"算)。

        ⚠️ 不能写成"第一个中线在鼠标下面的行": 正好停在某张卡片的中线上时
        那个判断既不成立也不往下走, 于是拖到中线反而**不动**(看着像卡住了)。
        这里是"数一数鼠标越过了几条中线" —— 往下拖、往上拖都一样灵。
        """
        n = self.list.count()
        if n <= 0:
            return 0
        target = 0
        for i in range(n):
            it = self.list.item(i)
            if it is None:
                continue
            r = self.list.visualItemRect(it)
            if y > r.center().y():
                target = i + 1
            else:
                break
        return max(0, min(target, n - 1))

    def _move_item(self, src: int, dst: int):
        """把第 src 张卡片挪到第 dst 张的位置(列表和 _clips 一起挪)。"""
        n = self.list.count()
        if not (0 <= src < n) or not (0 <= dst < n) or src == dst:
            return
        self._syncing = True
        try:
            it = self.list.takeItem(src)
            if it is None:
                return
            self.list.insertItem(dst, it)
            self.list.setCurrentRow(dst)
            c = self._clips.pop(src)
            self._clips.insert(dst, c)
        finally:
            self._syncing = False

    def _auto_scroll(self, y: int):
        """拖到上下沿: 自动滚一点, 不然长列表拖不到头。"""
        try:
            bar = self.list.verticalScrollBar()
            h = self.list.viewport().height()
            v = bar.value()
            if y < AUTO_PX and v > bar.minimum():
                bar.setValue(max(bar.minimum(), v - 24))
            elif y > h - AUTO_PX and v < bar.maximum():
                bar.setValue(min(bar.maximum(), v + 24))
        except Exception:
            pass


class ClipListBox(QFrame):
    """给主窗口一个带边框的外壳(和素材框视觉一致)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.inner = ClipListPanel(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.inner)
