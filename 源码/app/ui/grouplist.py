# -*- coding: utf-8 -*-
"""照片网格视图: 自动换行布局 + 自定义拖拽排序 + 橡皮筋框选。

布局技巧: 用 ListMode + Wrapping, 标题行的 sizeHint 宽度撑满视口,
于是它会自然独占一行, 下面的缩略图继续按宽度自动换行。
"""
from __future__ import annotations

from PySide6.QtCore import (QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QLabel, QListView, QWidget

from .delegate import ThumbDelegate, hit_test_header
from .widgets import Marquee
from ..core.photo import HeaderRow, PhotoModel, PhotoRow, THUMB_NONE


class _VirtualCursor(QWidget):
    """拖动时显示的虚拟光标圆点。跟随鼠标但 clamp 在父 widget 内,
    鼠标移出窗口时圆点停在边缘。半透明蓝色圆 + 白边。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(24, 24)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # 不拦截鼠标事件
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(2, 2, -2, -2)
        # 外圈白边
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawEllipse(r)
        # 内圈蓝色半透明
        inner = r.adjusted(3, 3, -3, -3)
        p.setBrush(QColor(59, 138, 221, 220))  # T.ACCENT 蓝
        p.drawEllipse(inner)
        p.end()


class GroupView(QListView):
    toggleCollapse = Signal(tuple)
    toggleSwitch = Signal(int, str, bool)
    orderChanged = Signal()
    needThumbs = Signal(list)
    selectionChangedEx = Signal(int)
    deleteRequested = Signal()      # 按 DEL：请求删除选中项（由主窗口二次确认后执行）

    # 长按启动拖动的阈值(按下后 _LONG_PRESS_MS 毫秒内不松手也进入拖动状态)
    _LONG_PRESS_MS = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ListMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setSpacing(6)
        self.setUniformItemSizes(False)
        self.setSelectionMode(QListView.ExtendedSelection)
        self.setSelectionBehavior(QListView.SelectItems)
        # 拖拽重排: 自写 mouseMove 实时让位(参考 NavEditor 卡片拖拽方案)
        # — mousedown + 第一次 mousemove 立即 dragging + 用"光标相对被拖批中心
        # 的方向"决定 before/after, 不再用"光标在目标卡的左/右半"判(那对纵向拖动失效)。
        self.setDragEnabled(False)
        self.setAcceptDrops(True)      # 外部拖文件进来时交给主窗口
        self.setDropIndicatorShown(False)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.setFrameShape(QListView.NoFrame)
        self.setContextMenuPolicy(Qt.CustomContextMenu)

        self.delegate = ThumbDelegate(self)
        self.setItemDelegate(self.delegate)

        self._rubber: Marquee | None = None
        self._rubber_origin = QPoint()
        self._rubber_additive = False
        self._press_index = -1
        self._press_pos = QPoint()

        # 拖拽运行时状态(参考 NavEditor _cd)
        self._live = False
        self._live_ids: set = set()
        self._live_snapshot: list = []
        self._live_pos: QPoint | None = None
        self._last_hit = None       # (target_row, after) 防重复触发
        self._auto_dir = 0
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(30)
        self._auto_timer.timeout.connect(self._auto_scroll_step)

        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(120)
        self._thumb_timer.timeout.connect(self.request_visible_thumbs)
        self.verticalScrollBar().valueChanged.connect(lambda _=0: self._thumb_timer.start())
        self.installEventFilter(self)

        # Ctrl+滚轮缩放回调: 参数 True=放大 / False=缩小（由主窗口注入 _zoom_main_step）
        self._on_zoom = None

        # 虚拟光标圆点(拖动时显示, clamp 在 viewport 内)
        self._vcursor = _VirtualCursor(self.viewport())
        self._vcursor.hide()

    # ---------- 模型 ----------
    def photo_model(self) -> PhotoModel | None:
        m = self.model()
        return m if isinstance(m, PhotoModel) else None

    def setModel(self, model):
        super().setModel(model)
        if model is not None:
            model.rowsInserted.connect(lambda *a: self._thumb_timer.start())
            model.modelReset.connect(lambda: self._thumb_timer.start())
        self._thumb_timer.start()

    def _row(self, i: int):
        m = self.photo_model()
        return m.rows[i] if m and 0 <= i < len(m.rows) else None

    def _is_photo(self, i: int) -> bool:
        return isinstance(self._row(i), PhotoRow)

    # ---------- 缩略图 ----------
    def request_visible_thumbs(self, extra: list | None = None):
        m = self.photo_model()
        if m is None:
            return
        top = self.indexAt(QPoint(4, 4))
        bottom = self.indexAt(QPoint(4, max(0, self.viewport().height() - 4)))
        start = top.row() if top.isValid() else 0
        end = bottom.row() if bottom.isValid() else len(m.rows) - 1
        if end < start:
            start, end = 0, len(m.rows) - 1
        start = max(0, start - 8)
        end = min(len(m.rows) - 1, end + 24)
        paths = []
        for i in range(start, end + 1):
            row = m.rows[i] if i < len(m.rows) else None
            if isinstance(row, PhotoRow) and row.photo.thumb_state == THUMB_NONE:
                paths.append(row.photo.path)
        if extra:
            paths.extend(extra)
        if paths:
            self.needThumbs.emit(paths)

    # ---------- 鼠标 ----------
    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        if idx.isValid():
            row = self._row(idx.row())
            if isinstance(row, HeaderRow):
                hit = hit_test_header(self.visualRect(idx), row, pos)
                if hit == "collapse":
                    self.toggleCollapse.emit(tuple(row.key))
                    return
                if hit and hit.startswith("toggle:"):
                    which = hit.split(":", 1)[1]
                    enabled = row.month_enabled if which == "month" else row.day_enabled
                    if enabled:
                        current = row.month_on if which == "month" else row.day_on
                        self.toggleSwitch.emit(row.year, which, not current)
                    return
                return  # 点在标题空白处: 什么都不做
        if not idx.isValid():
            # 空白区域: 启动橡皮筋框选
            mods = event.modifiers()
            self._rubber_additive = bool(mods & Qt.ControlModifier or mods & Qt.ShiftModifier)
            if not self._rubber_additive:
                self.clearSelection()
            self._rubber_origin = pos
            self._rubber = Marquee(self.viewport())
            self._rubber.setGeometry(QRect(pos, QSize()))
            self._rubber.show()
            return
        self._press_index = idx.row()
        self._press_pos = pos
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._rubber is not None:
            pos = event.position().toPoint()
            rect = QRect(self._rubber_origin, pos).normalized()
            self._rubber.setGeometry(rect)
            self._select_in_rect(rect)
            return
        pos = event.position().toPoint()
        if self._live:
            self._live_move(pos)
            return
        # 第一次移动立即进入拖动(无阈值, 灵敏度拉满 — 参考 NavEditor)
        if (event.buttons() & Qt.LeftButton) and self._press_index >= 0:
            if self._begin_live(pos):
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._rubber is not None:
            self._rubber.hide()
            self._rubber.deleteLater()
            self._rubber = None
            return
        if self._live:
            super().mouseReleaseEvent(event)
            self._end_live(True)
            return
        self._press_index = -1
        super().mouseReleaseEvent(event)

    def _select_in_rect(self, rect: QRect):
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        m = self.photo_model()
        if m is None:
            return
        sel = QItemSelection()
        for i in range(len(m.rows)):
            if not self._is_photo(i):
                continue
            vr = self.visualRect(m.index(i))
            if vr.intersects(rect):
                idx = m.index(i)
                sel.select(idx, idx)
        mode = QItemSelectionModel.Select if self._rubber_additive else QItemSelectionModel.ClearAndSelect
        self.selectionModel().select(sel, mode)

    # ---------- 拖拽实时让位(参考 NavEditor 卡片拖拽方案) ----------
    def _begin_live(self, pos: QPoint) -> bool:
        m = self.photo_model()
        if m is None:
            return False
        idx = self.indexAt(pos)
        if not idx.isValid() or not self._is_photo(idx.row()):
            return False
        rows = sorted({i.row() for i in self.selectedIndexes() if self._is_photo(i.row())})
        if idx.row() not in rows:
            from PySide6.QtCore import QItemSelectionModel
            self.selectionModel().select(m.index(idx.row()),
                                         QItemSelectionModel.ClearAndSelect)
            rows = [idx.row()]
        if not rows:
            return False
        self._live_ids = {id(self._row(r).photo) for r in rows}
        self._live_snapshot = list(m.photos)
        # 确保起点时布局已是最新(executeDelayedItemsLayout 强制立即布局)
        self.executeDelayedItemsLayout()
        self._last_hit = None
        self._auto_dir = 0
        self._live = True
        # 隐藏系统光标, 显示虚拟光标圆点
        self.setCursor(Qt.BlankCursor)
        self._vcursor.show()
        self._move_vcursor(pos)
        self.viewport().update()
        return True

    def _end_live(self, commit: bool):
        if not self._live:
            return
        self.setCursor(Qt.ArrowCursor)
        self._vcursor.hide()
        self._auto_timer.stop()
        self._auto_dir = 0
        m = self.photo_model()
        if not commit and m is not None:
            m.photos = self._live_snapshot
            m.rebuild()
        self._live = False
        self._live_pos = None
        self._live_snapshot = []
        if m is not None:
            self._reselect_live()
        self._live_ids = set()
        self._press_index = -1
        self._last_hit = None
        self.viewport().update()
        if commit:
            self.orderChanged.emit()

    def _grid_after(self, pos: QPoint, vr: QRect) -> bool:
        """网格阅读顺序判断: 光标应插在该卡之前(False)还是之后(True)。

        规则(对换行网格才正确, 之前的"相对被拖批中心方向"在跨行时必错):
          - 光标 y 在该卡上边缘之上 → 之前(上方那一行)
          - 光标 y 在该卡下边缘之下 → 之后(下方那一行)
          - 同行(落在卡的高度带内) → 以卡中心 x 为界: 左半 before / 右半 after
        """
        if pos.y() < vr.top():
            return False
        if pos.y() > vr.bottom():
            return True
        return pos.x() >= vr.center().x()

    def _resolve_target(self, pos: QPoint, idx, cur: list) -> tuple:
        """根据光标位置算要插入到哪张卡(target_row)之前/之后(after)。

        返回 (target_row, after); 无法定位时返回 (None, False)。
        """
        m = self.photo_model()
        if not idx.isValid():
            # 空白: 取几何中心最近的非 cur 卡, 仍用网格规则判 before/after
            best, best_d = -1, 1 << 30
            for i, r in enumerate(m.rows):
                if not isinstance(r, PhotoRow) or i in cur:
                    continue
                vr = self.visualRect(m.index(i))
                if not vr.isValid():
                    continue
                dx = vr.center().x() - pos.x()
                dy = vr.center().y() - pos.y()
                d = dx * dx + dy * dy
                if d < best_d:
                    best_d, best = d, i
            if best < 0:
                return None, False
            vr = self.visualRect(m.index(best))
            return best, self._grid_after(pos, vr)
        r = self._row(idx.row())
        if isinstance(r, HeaderRow):
            # 标题: 上半→上一组末尾 after, 下半→本组首卡 before
            vr = self.visualRect(idx)
            upper = pos.y() < vr.center().y()
            if upper:
                j = idx.row() - 1
                while j >= 0 and not isinstance(m.rows[j], PhotoRow):
                    j -= 1
                if j < 0:
                    return None, False
                return j, True
            else:
                j = idx.row() + 1
                while j < len(m.rows) and not isinstance(m.rows[j], PhotoRow):
                    j += 1
                if j >= len(m.rows):
                    return None, False
                return j, False
        if isinstance(r, PhotoRow):
            if idx.row() in cur:
                return None, False
            vr = self.visualRect(idx)
            return idx.row(), self._grid_after(pos, vr)
        return None, False

    def _move_vcursor(self, pos: QPoint):
        """把虚拟光标圆点移到 pos(clamp 到 viewport 内), 圆点居中对齐 pos。"""
        vw = self.viewport().width()
        vh = self.viewport().height()
        cx = max(0, min(vw - 1, pos.x()))
        cy = max(0, min(vh - 1, pos.y()))
        sz = self._vcursor.width()
        self._vcursor.move(cx - sz // 2, cy - sz // 2)
        self._vcursor.update()

    def _live_move(self, pos: QPoint):
        """实时让位 — 网格阅读顺序定位:

        1. 鼠标还在被拖批任一卡矩形内 → 不动(避免微动误跳)
        2. 用 indexAt 拿光标命中的卡, 交给 _resolve_target 用网格规则
           (先比行 y, 同行再比列 x) 算出"插到哪张卡之前/之后"
        3. 同 (target, after) 不重复触发; 紧贴 cur 边界说明已就位, 不动
        """
        self._live_pos = pos
        m = self.photo_model()
        if m is None:
            return
        # 更新虚拟光标位置(clamp 在 viewport 内)
        self._move_vcursor(pos)
        # 用原始 pos 判自动滚动(光标在窗口外也要滚)
        self._update_auto_scroll(pos)
        # 把 pos clamp 到 viewport 矩形内(鼠标移出窗口后 Qt 仍触发 mouseMove,
        # pos 超出 viewport 会让 indexAt/visualRect 判定全乱 → clamp 后贴边缘、判定有效)
        vw = self.viewport().width()
        vh = self.viewport().height()
        pos = QPoint(max(0, min(vw - 1, pos.x())),
                     max(0, min(vh - 1, pos.y())))
        cur = [i for i, r in enumerate(m.rows)
               if isinstance(r, PhotoRow) and id(r.photo) in self._live_ids]
        if not cur:
            return
        # 1. 鼠标还在被拖批任一卡矩形内 → 不动
        for i in cur:
            vr = self.visualRect(m.index(i))
            if vr.isValid() and vr.contains(pos):
                self._last_hit = None
                return
        # 2. 解析插入目标
        idx = self.indexAt(pos)
        target_row, after = self._resolve_target(pos, idx, cur)
        if target_row is None or target_row in cur:
            return
        # 同 (target, after) 不重复触发
        if self._last_hit is not None and \
           self._last_hit[0] == target_row and self._last_hit[1] == after:
            return
        self._last_hit = (target_row, after)
        # 紧贴 cur 边界 → 已在该位置
        if after and (target_row + 1) in cur:
            return
        if (not after) and (target_row - 1) in cur:
            return
        sb = self.verticalScrollBar().value()
        m.move_rows(cur, target_row, after, live=True)
        # 强制 Qt 立即重排布局(rebuild 是 beginResetModel/endResetModel,
        # 布局默认延迟到事件循环 — 不强制的话 visualRect 拿到旧值 → 定位错/震荡)
        self.executeDelayedItemsLayout()
        self.verticalScrollBar().setValue(sb)
        self._reselect_live()

    def _reselect_live(self):
        m = self.photo_model()
        if m is None or not self._live_ids:
            return
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        sel = QItemSelection()
        for i, r in enumerate(m.rows):
            if isinstance(r, PhotoRow) and id(r.photo) in self._live_ids:
                idx = m.index(i)
                sel.select(idx, idx)
        if sel.count():
            self.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)

    # --- 自动滚动(拖到列表上下边缘时) ---
    def _update_auto_scroll(self, pos: QPoint):
        h = self.viewport().height()
        if pos.y() < 36:
            self._auto_dir = -1
        elif pos.y() > h - 36:
            self._auto_dir = 1
        else:
            self._auto_dir = 0
        if self._auto_dir and not self._auto_timer.isActive():
            self._auto_timer.start()
        elif not self._auto_dir and self._auto_timer.isActive():
            self._auto_timer.stop()

    def _auto_scroll_step(self):
        sb = self.verticalScrollBar()
        step = max(8, sb.pageStep() // 10)
        sb.setValue(sb.value() + self._auto_dir * step)
        if self._live_pos is not None:
            self._live_move(self._live_pos)

    # 外部(资源管理器)拖文件进来: 一律往上抛, 交给主窗口处理
    def dragEnterEvent(self, event):
        event.ignore()

    def dragMoveEvent(self, event):
        event.ignore()

    def dropEvent(self, event):
        event.ignore()

    # ---------- 其它 ----------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scheduleDelayedItemsLayout()
        self._thumb_timer.start()

    def wheelEvent(self, event):
        if (event.modifiers() & Qt.ControlModifier) and self._on_zoom is not None:
            self._on_zoom(event.angleDelta().y() > 0)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        if self._live and event.key() == Qt.Key_Escape:
            self._end_live(False)
            return
        if event.key() == Qt.Key_A and event.modifiers() & Qt.ControlModifier:
            self.select_all_photos()
            return
        if event.key() == Qt.Key_Delete and self.selectedIndexes():
            # 焦点在网格上时按 DEL：交给主窗口做二次确认后删除
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)

    def select_all_photos(self):
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        m = self.photo_model()
        if m is None:
            return
        sel = QItemSelection()
        for i in range(len(m.rows)):
            if self._is_photo(i):
                idx = m.index(i)
                sel.select(idx, idx)
        if sel.count():
            self.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)

    def selected_photos(self) -> list:
        m = self.photo_model()
        if m is None:
            return []
        return m.selected_photos([i for i in self.selectedIndexes()])

    def selectionCount(self) -> int:
        return sum(1 for i in self.selectedIndexes() if self._is_photo(i.row()))

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QEvent.Wheel:
            self._thumb_timer.start()
        return super().eventFilter(obj, event)
