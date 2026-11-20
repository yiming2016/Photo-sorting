# -*- coding: utf-8 -*-
"""照片网格视图: 自动换行布局 + 自定义拖拽排序 + 橡皮筋框选。

布局技巧: 用 ListMode + Wrapping, 标题行的 sizeHint 宽度撑满视口,
于是它会自然独占一行, 下面的缩略图继续按宽度自动换行。
"""
from __future__ import annotations

from PySide6.QtCore import (QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QLabel, QListView, QWidget

from . import theme as T
from .delegate import ThumbDelegate, hit_test_header, hit_test_pack, pack_header_h
from ..core.photo import (CollapsedPack, HeaderRow, PackRow, PhotoModel,
                          PhotoRow, THUMB_NONE)


def _switch_state(row, which: str) -> tuple[bool, bool]:
    """统一页标题上的 分辨率/帧率 开关:(当前是否打开, 是否可点)。

    排序模式的 月/日 状态直接挂在 HeaderRow 上; 统一页同理, 只是字段名不同。
    """
    if which == "res":
        return bool(getattr(row, "norm_res_on", False)), bool(getattr(row, "norm_res_enabled", True))
    return bool(getattr(row, "norm_fps_on", False)), bool(getattr(row, "norm_fps_enabled", True))


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
    normToggle = Signal(tuple, str, bool)   # 统一页组标题的 分辨率/帧率 开关(参数: 组key, which, 新状态)
    toggleGroupCheck = Signal(tuple)    # 合并模式下点了组标题的"全选本组"勾选框
    orderChanged = Signal()
    needThumbs = Signal(list)
    selectionChangedEx = Signal(int)
    deleteRequested = Signal()      # 按 DEL：请求删除选中项（由主窗口二次确认后执行）
    # v231 统一页多文件组块: 子卡逐张点击(路径级勾选) / 双击打开某一张
    gridPathPress = Signal(str, bool, bool)  # (路径, 是否加选(Ctrl/Shift), 是否右键)
    openSubPhoto = Signal(object)            # (Photo,)

    # 长按启动拖动的阈值(按下后 _LONG_PRESS_MS 毫秒内不松手也进入拖动状态)
    _LONG_PRESS_MS = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        # v216: 本控件**自己**按区域换光标(_hover_cursor: 照片卡/折叠三角/开关=手,
        # 标题行空白处=箭头), 比"整片手"更准 → 让全局那条规则别插手。
        self.setProperty("handCursor", False)
        self.setViewMode(QListView.ListMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setSpacing(2)
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

        # 是否正在拉框。框不做成子控件, 而是由 paintEvent 直接画在视口上 ——
        # 子控件(QRubberBand)得靠每条滚动路径手动搬位置, 漏一条(或被事件路由
        # 干扰)框就不跟着滚轮走; 改成视图自己画, 矩形按内容坐标存、绘制时再
        # 换算, 任何滚动之后重绘都会自动把框画回同一个内容位置, 永远跟着内容走。
        self._rubber_on = False
        # 框选矩形记在"内容坐标"里(不是屏幕坐标): 滚动页面时框住的始终是同一批照片,
        # 不会出现"框选后滚一下, 原来框的被冲掉"。视觉上矩形跟着内容走。
        self._rubber_rect = QRect()
        self._rubber_origin_c = QPoint()   # 按下点(内容坐标), 拖动过程中固定不变
        self._rubber_end_c = QPoint()      # 框选终点(内容坐标)
        # 鼠标最后所在的"视口坐标"。终点一律由它 + 当前滚动偏移算出来,
        # 于是: 鼠标不动只滚滚轮 → 终点的内容坐标跟着滚动量走 → 框继续往下/往上
        # 长("滚到哪框到哪"); 鼠标动了 → 按新位置重算, 与上次连续, 不会跳。
        self._rubber_mouse_v = QPoint()
        self._rubber_additive = False
        self._press_index = -1
        self._press_pos = QPoint()
        self._merge_toggle_idx = None   # 合并模式: 上次单击切换过勾选的那一格(双击时还原)
        self._hover_sub_key_last = None  # v231: 鼠标所在块内子卡的标识(切换时触发重绘)

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
        # 滚动: ① 补加载可见区域的缩略图; ② 框选没松手时, 矩形按内容坐标锚定,
        # 滚动只是让它视觉上跟着内容走, 选中集合保持不变(以前按屏幕坐标记,
        # 滚动后紧接着一挪鼠标就会把之前框选的整批冲掉 → "框选丢失")
        for _sb in (self.horizontalScrollBar(), self.verticalScrollBar()):
            _sb.valueChanged.connect(self._on_scrolled)
        self.installEventFilter(self)

        # Ctrl+滚轮缩放回调: 参数 True=放大 / False=缩小（由主窗口注入 _zoom_main_step）
        self._on_zoom = None

        # 合并模式: 顶部勾选框/规格行由 delegate 画; 拖拽只允许在同一规格组内调整先后
        self.merge_mode = False
        # v233: 统一页也借用 merge_mode=True 渲染网格, 但**勾选语义**必须是路径级
        # (与 v231 组块子卡同一套)。这个标志让 press/doubleClick 区分两种形态:
        # merge=点卡即勾/取消(不清别人), norm=左键"只勾这张"、右键补勾。
        self.norm_mode = False

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

    def _hit_photo(self, pos: QPoint) -> bool:
        """鼠标位置是否真的落在某个照片格子上(空白区域返回 False)。"""
        idx = self.indexAt(pos)
        return idx.isValid() and self._is_photo(idx.row())

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
            if isinstance(row, PackRow):
                # v231: 组块内每张子卡的缩略图都要单独请求
                for sub in row.all_rows():
                    if sub.photo.thumb_state == THUMB_NONE:
                        paths.append(sub.photo.path)
            elif isinstance(row, PhotoRow) and row.photo.thumb_state == THUMB_NONE:
                paths.append(row.photo.path)
        if extra:
            paths.extend(extra)
        if paths:
            self.needThumbs.emit(paths)

    # ---------- 鼠标 ----------
    def _pack_head_h(self, row: PackRow, vr) -> int:
        """紧凑卡头区高度(点击路由用): 上半组头 / 下半照片的分界线。

        v228: 三态统一头高(delegate._pack_uni_h, 与 sizeHint/draw_pack 同源)。
        合并独立卡收起时没有照片, 整卡都是头。
        """
        hh = pack_header_h(row.header, vr.width())
        uni = self.delegate._pack_uni_h()
        if row.header.merge_tag and row.header.collapsed:
            return vr.height()
        if row.header.merge_tag:
            return max(hh, uni, vr.height() - self.delegate.cell_h)
        return max(hh, uni)

    def _hit_header(self, row, idx, pos):
        """组标题命中(v230 窄标题块已移除, 统一走满宽标题布局)。"""
        return hit_test_header(self.visualRect(idx), row, pos)

    def _pack_sub_at(self, row: PackRow, vr: QRect, pos: QPoint):
        """多文件组块: 鼠标位置落在哪张子卡上(返回该 PhotoRow), 空隙返回 None。

        坐标口径与 delegate._pack_geo / draw_pack 完全一致(列距 = 卡宽+间距,
        行距 = 卡高+间距, 起点 = 块左缘 / 头区下缘)。
        """
        subs = row.all_rows()
        if len(subs) <= 1:
            return None
        D = self.delegate
        geo = D._pack_geo(row)
        pitch = D._pack_pitch()
        head_h = self._pack_head_h(row, vr)
        col = (pos.x() - vr.x()) // pitch
        rw = (pos.y() - vr.y() - head_h) // (D.cell_h + geo["gap"])
        if col < 0 or col >= geo["cols"] or rw < 0 or rw >= geo["nrows"]:
            return None
        sr = QRect(vr.x() + col * pitch,
                   vr.y() + head_h + rw * (D.cell_h + geo["gap"]),
                   D.cell_w, D.cell_h)
        if not sr.contains(pos):
            return None
        i = rw * geo["cols"] + col
        return subs[i] if i < len(subs) else None

    def _hover_sub_key(self, pos: QPoint):
        """鼠标所在的多文件组块子卡标识(行号 + 块内序号); 不在块上返回 None。"""
        idx = self.indexAt(pos)
        if not idx.isValid():
            return None
        row = self._row(idx.row())
        if not isinstance(row, PackRow) or len(row.all_rows()) <= 1:
            return None
        sub = self._pack_sub_at(row, self.visualRect(idx), pos)
        if sub is None:
            return None
        ids = [id(r.photo) for r in row.all_rows()]
        return (idx.row(), ids.index(id(sub.photo)))

    def mousePressEvent(self, event):
        # 收尸: 极少数情况(松手事件没送到本控件, 例如在窗外松手)会残留上一个
        # 框选矩形。先清掉再处理本次按下, 否则旧的框会一直挂在屏幕上。
        if self._rubber_on:
            self._rubber_on = False
            self._rubber_rect = QRect()
            self._rubber_mouse_v = QPoint()
            self.viewport().update()
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        # v236: 左键一进来就把"拖拽起点"清掉 —— 组标题/组块/空白这些拦截分支
        # 都会 return, 若沿用上一次点照片残留的 _press_index(>=0), 下一次带键
        # 移动会被 mouseMoveEvent 误判成拖拽(_begin_live → ClearAndSelect),
        # 表现就是"点了组头/空白处, 鼠标一动就选中了很远的一批文件"。
        self._press_index = -1
        self._press_pos = pos
        # 右键单独处理: 只"补选中", 绝不取消已选、不折叠分组、不碰年月开关。
        # 以前右键会走下面同一个分支, 把已选中的那一格取消掉 → 右键瞬间变空选,
        # 菜单就跳成"没有选中项"那一份(全选可无损 / 全选所有视频)。
        if event.button() != Qt.LeftButton:
            row = self._row(idx.row()) if idx.isValid() else None
            if isinstance(row, PackRow) and len(row.all_rows()) > 1:
                # v231 多文件组块: 右键 = 补选中那一**张**子卡(路径级),
                # 绝不整块全选 —— 和单张照片右键的口径一致。
                sub = self._pack_sub_at(row, self.visualRect(idx), pos)
                if sub is not None:
                    from PySide6.QtCore import QItemSelectionModel
                    self.selectionModel().setCurrentIndex(
                        idx, QItemSelectionModel.NoUpdate)
                    self.gridPathPress.emit(sub.photo.path, False, True)
                self._press_index = -1
                self._press_pos = pos
                return
            if getattr(self, "norm_mode", False) and idx.isValid() \
                    and self._is_photo(idx.row()):
                # v233: 统一页右键 = 补勾这一格(与组块子卡同口径), 不碰 Qt 选中。
                # 以前走合并式的 Qt select(ClearAndSelect/Select), 会把其它组
                # 已勾的可见卡冲掉 —— 与"右键绝不取消已勾"的口径冲突。
                self.gridPathPress.emit(row.photo.path, False, True)
                self._press_index = -1
                self._press_pos = pos
                return
            if idx.isValid() and self._is_photo(idx.row()):
                from PySide6.QtCore import QItemSelectionModel
                sm = self.selectionModel()
                # 注意: 不能用 setCurrentIndex() —— 它内部按 ClearAndSelect 处理, 会把
                # 已勾选的其它格全清掉。用 selectionModel 的 NoUpdate 只挪光标, 不动选中。
                sm.setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
                if not sm.isSelected(idx):
                    flag = (QItemSelectionModel.Select
                            if event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)
                            else QItemSelectionModel.ClearAndSelect)
                    sm.select(idx, flag)
            self._press_index = -1
            self._press_pos = pos
            return
        if idx.isValid():
            row = self._row(idx.row())
            if isinstance(row, CollapsedPack):
                # v232: 收起的叶级组块 —— 整格都是头块: 勾选框=全选本组,
                # 其余任意位置(含三角)=展开。与展开组头的命中口径一致。
                hit = hit_test_pack(self.visualRect(idx), row.header, pos)
                if hit == "groupcheck":
                    self.toggleGroupCheck.emit(tuple(row.header.key))
                else:
                    self.toggleCollapse.emit(tuple(row.header.key))
                return
            if isinstance(row, PackRow):
                # v226 紧凑子组卡: 上半是组头(折叠/全选本组), 下半当照片走
                vr = self.visualRect(idx)
                hh = self._pack_head_h(row, vr)
                if pos.y() < vr.y() + hh:
                    hit = hit_test_pack(QRect(vr.x(), vr.y(), vr.width(), hh),
                                        row.header, pos)
                    if hit == "groupcheck":
                        self.toggleGroupCheck.emit(tuple(row.header.key))
                        return
                    if hit == "collapse":
                        self.toggleCollapse.emit(tuple(row.header.key))
                        return
                    return
                if len(row.all_rows()) > 1:
                    # v231 多文件组块: 下半部逐张子卡 —— 单击=只勾这张 /
                    # Ctrl·Shift=加减这张(路径级, 由主窗口落到 _grid_sel_paths)。
                    # 拦下不调 super: 条目级选中表达不了"块内只勾一部分"。
                    sub = self._pack_sub_at(row, vr, pos)
                    if sub is not None:
                        additive = bool(event.modifiers()
                                        & (Qt.ControlModifier | Qt.ShiftModifier))
                        self.gridPathPress.emit(sub.photo.path, additive, False)
                    return    # 子卡之间的空隙: 什么都不做
            elif isinstance(row, HeaderRow):
                hit = self._hit_header(row, idx, pos)
                if hit == "groupcheck":
                    self.toggleGroupCheck.emit(tuple(row.key))
                    return
                if hit == "collapse":
                    self.toggleCollapse.emit(tuple(row.key))
                    return
                if hit and hit.startswith("toggle:"):
                    which = hit.split(":", 1)[1]
                    if row.norm_mode:
                        on, ok = _switch_state(row, which)
                        if ok:
                            self.normToggle.emit(tuple(row.key), which, not on)
                        return
                    enabled = row.month_enabled if which == "month" else row.day_enabled
                    if enabled:
                        current = row.month_on if which == "month" else row.day_on
                        self.toggleSwitch.emit(row.year, which, not current)
                    return
                return  # 理论兜底: 命中不是以上任何一种就什么都不做
        if not idx.isValid():
            # 空白区域: 启动橡皮筋框选
            mods = event.modifiers()
            self._rubber_additive = bool(mods & Qt.ControlModifier or mods & Qt.ShiftModifier)
            if not self._rubber_additive:
                self.clearSelection()
            # 起点钉死在内容坐标上(滚动不会让它跑), 终点永远等于
            # "鼠标当前所在的视口位置 + 当前滚动偏移": 于是滚滚轮时终点跟着内容挪,
            # 框就一直长到新露出来的那一片 —— 滚到哪框到哪。
            off = self._scroll_offset()
            self._rubber_origin_c = QPoint(pos.x() + off.x(), pos.y() + off.y())
            self._rubber_end_c = QPoint(self._rubber_origin_c)
            self._rubber_mouse_v = QPoint(pos)
            self._rubber_rect = QRect(self._rubber_origin_c, QSize())
            self._rubber_on = True
            self.viewport().update()
            return
        # v233: 统一页所有卡片(单文件紧凑卡/普通照片格)与多文件组块子卡走同一套
        # 路径级勾选: 左键=只勾这张(收起组里的勾选保留), Ctrl/Shift=加减这张。
        # 以前单卡走合并式的"点谁谁加选"、普通格走 Qt 的 ClearAndSelect,
        # 三种卡口径不一致 —— 用户看到"点 A 连带 B 被勾/取消"且找不出规律。
        # ⚠️ 放在 _press_index 赋值之前: 统一页不支持拖拽(组块 v231 起也不支持),
        # 不给拖拽起点留入口 —— _begin_live 的 ClearAndSelect 会把其它组已勾的冲掉。
        if getattr(self, "norm_mode", False) and self._is_photo(idx.row()):
            additive = bool(event.modifiers()
                            & (Qt.ControlModifier | Qt.ShiftModifier))
            self.gridPathPress.emit(self._row(idx.row()).photo.path,
                                    additive, False)
            return
        self._press_index = idx.row()
        self._press_pos = pos
        # 合并模式: 单击 = 勾选/取消勾选这一格(像真勾选框那样可以反复点),
        # 不清空其它已勾选的。不调 super().mousePressEvent,
        # 避免 Qt 默认的"点谁就只留谁"把其它勾选冲掉。
        if self.merge_mode and self._is_photo(idx.row()):
            if not (event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
                from PySide6.QtCore import QItemSelectionModel
                sm = self.selectionModel()
                # 合并态不调 super, 手动挪光标(NoUpdate: 只挪光标, 不改选中),
                # 光标 = 当前参照格(右键菜单里"以这一格为准"的操作都读它)
                sm.setCurrentIndex(idx, QItemSelectionModel.NoUpdate)
                # 已选 -> 取消(不管当前一共选了几个); 未选 -> 加选
                flag = (QItemSelectionModel.Deselect if sm.isSelected(idx)
                        else QItemSelectionModel.Select)
                sm.select(idx, flag)
                self._merge_toggle_idx = idx
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击图片/视频 = 用系统默认程序打开。

        自己 emit doubleClicked: 合并模式下 press 不调 super, Qt 内部没有记录
        pressedIndex, 默认实现就不会发这个信号(以前双击没反应就是这个原因)。
        """
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        if idx.isValid():
            row = self._row(idx.row())
            if isinstance(row, PackRow):
                vr = self.visualRect(idx)
                hh = self._pack_head_h(row, vr)
                if pos.y() < vr.y() + hh:
                    return      # 组头区的两次按下已经在 press 里各自折叠过一次, 双击不动作
                if len(row.all_rows()) > 1:
                    # v231 多文件组块: 双击 = 打开双击的那**张**子卡
                    sub = self._pack_sub_at(row, vr, pos)
                    if sub is not None:
                        self.openSubPhoto.emit(sub.photo)
                    return
        if idx.isValid() and self._is_photo(idx.row()):
            if (self.merge_mode and self._merge_toggle_idx is not None
                    and self._merge_toggle_idx == idx):
                # 双击时第一下 press 已经切换过勾选, 还原它: 双击只负责"打开"
                from PySide6.QtCore import QItemSelectionModel
                sm = self.selectionModel()
                flag = (QItemSelectionModel.Deselect if sm.isSelected(idx)
                        else QItemSelectionModel.Select)
                sm.select(idx, flag)
            self._merge_toggle_idx = None
            self.doubleClicked.emit(idx)
            return
        super().mouseDoubleClickEvent(event)

    # ---------- 悬浮光标 ----------
    def _hover_cursor(self, pos: QPoint) -> Qt.CursorShape:
        """鼠标停在这儿该是什么光标: 能点/能拖的地方一律抓手, 其余普通箭头。

        ⚠️ 这里的"抓手"是 PointingHandCursor(和顶栏那些按钮一致), 不是
        OpenHandCursor —— 后者是"按住拖着走", 只有可拖动的预览图才用。
        """
        idx = self.indexAt(pos)
        if not idx.isValid():
            return Qt.ArrowCursor
        row = self._row(idx.row())
        if row is None:
            return Qt.ArrowCursor
        if isinstance(row, HeaderRow):
            hit = self._hit_header(row, idx, pos)
            if hit is None:
                return Qt.ArrowCursor                  # 理论上不会走到(v218 起整行都可折叠)
            if hit.startswith("toggle:"):
                which = hit.split(":", 1)[1]
                if row.norm_mode:
                    _on, ok = _switch_state(row, which)
                    return Qt.PointingHandCursor if ok else Qt.ArrowCursor
                ok = row.month_enabled if which == "month" else row.day_enabled
                return Qt.PointingHandCursor if ok else Qt.ArrowCursor
            return Qt.PointingHandCursor               # 折叠三角 / 整行折叠区 / 「全选本组」
        if isinstance(row, CollapsedPack):
            # v232: 收起的叶级组块整格可点(展开/全选本组)
            return Qt.PointingHandCursor
        if not isinstance(row, PhotoRow):
            return Qt.ArrowCursor                      # RowBreak 等隐形条
        return Qt.PointingHandCursor                   # 照片卡片(按住能拖着排序)

    def _apply_hover_cursor(self, pos: QPoint):
        # 拖动时系统光标被藏起来了(只剩虚拟圆点), 框选中也别抢它的光标
        if self._live or self._rubber_on:
            return
        want = self._hover_cursor(pos)
        if self.cursor().shape() != want:
            self.setCursor(want)

    def leaveEvent(self, event):
        if not self._live:
            self.unsetCursor()
        return super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        self._apply_hover_cursor(pos)
        # v231: 块内子卡的逐张悬浮高亮 —— 鼠标在同一块里挪动时 Qt 不会重绘
        # (条目级 MouseOver 状态没变), 子卡之间的高亮切换要自己触发重绘
        if not self._rubber_on and not self._live:
            k = self._hover_sub_key(pos)
            if k != self._hover_sub_key_last:
                self._hover_sub_key_last = k
                self.viewport().update()
        # 鼠标挪到空白处时清掉残留的 tooltip, 否则会一直显示上一个文件名
        if not self._hit_photo(pos) and self.toolTip():
            self.setToolTip("")
            self.viewport().setToolTip("")
        if self._rubber_on:
            # 终点 = 鼠标视口位置 + 当前滚动偏移(每次都重算, 不累加位移)。
            # 累加位移的写法在滚过一次之后会和新偏移算出两个不同的终点, 框就乱跳。
            self._rubber_mouse_v = QPoint(pos)
            off = self._scroll_offset()
            self._rubber_end_c = QPoint(pos.x() + off.x(), pos.y() + off.y())
            self._rubber_rect = QRect(self._rubber_origin_c, self._rubber_end_c).normalized()
            self.viewport().update()
            self._select_in_rect(self._rubber_rect)
            return
        if self._live:
            self._live_move(pos)
            return
        # 第一次移动立即进入拖动(无阈值, 灵敏度拉满 — 参考 NavEditor)
        if (event.buttons() & Qt.LeftButton) and self._press_index >= 0:
            if self._begin_live(pos):
                return
        # v236: 本次左键不是从照片格按下(_press_index<0, 比如点在组标题/组块/
        # 空白上)时, 绝不把带键移动交给 Qt —— ExtendedSelection 会拿它内部
        # 残留的 press 锚点做"范围选中", 把鼠标经过的一大片远处文件全勾上,
        # 表现就是"点了组头/空白, 鼠标一动选中了很远的一批"。我们的选中口径
        # (单格点选/橡皮筋/路径级) 全在 press 里自己处理, 不需要 super 的扩展。
        if event.buttons() & (Qt.LeftButton | Qt.RightButton):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._rubber_on:
            self._rubber_on = False
            self._rubber_rect = QRect()
            self._rubber_mouse_v = QPoint()
            self.viewport().update()
            return
        if self._live:
            super().mouseReleaseEvent(event)
            self._end_live(True)
            return
        self._press_index = -1
        super().mouseReleaseEvent(event)

    # ---------- 框选(矩形按"内容坐标"记: 滚动页面不丢选中) ----------
    def _scroll_offset(self) -> QPoint:
        """当前滚动偏移(内容坐标 = 视口坐标 + 偏移)。"""
        return QPoint(self.horizontalScrollBar().value(),
                      self.verticalScrollBar().value())

    def _to_viewport(self, content_rect: QRect) -> QRect:
        off = self._scroll_offset()
        return content_rect.translated(-off.x(), -off.y())

    def _on_scrolled(self, *_):
        self._thumb_timer.start()
        if not self._rubber_on:
            return
        self._rubber_after_scroll()

    def _rubber_after_scroll(self):
        """拉框途中页面滚了(滚轮/拖滚动条/键盘翻页): 把终点按"鼠标还停在
        原来那个视口位置"重算 —— 内容往上挪了多少, 终点在内容里就跟着挪多少,
        框于是继续长到刚露出来的那一片(滚到哪框到哪); 起点钉在内容上不动。"""
        if not self._rubber_on or self._rubber_mouse_v is None:
            return
        off = self._scroll_offset()
        p = self._rubber_mouse_v
        self._rubber_end_c = QPoint(p.x() + off.x(), p.y() + off.y())
        self._rubber_rect = QRect(self._rubber_origin_c, self._rubber_end_c).normalized()
        self.viewport().update()
        self._select_in_rect(self._rubber_rect)

    def paintEvent(self, event):
        """先让 Qt 画完格子, 再把"拉框"画在最上面。

        框由视图自己画(而不是 QRubberBand 子控件): 矩形按内容坐标存,
        绘制时才减去滚动偏移 —— 于是不管页面是被滚轮、滚动条、自动滚动
        还是键盘翻页挪动的, 重绘时框都会自动出现在同一个内容位置上,
        永远跟着内容走, 不依赖任何一条滚动路径去手动搬它。
        """
        super().paintEvent(event)
        if not self._rubber_on or self._rubber_rect.isEmpty():
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing)
        r = self._to_viewport(self._rubber_rect)
        painter.setPen(Qt.NoPen)
        # ⚠️ "#RRGGBBAA" 不是 Qt 认的写法(8 位会被当 AARRGGBB 解析成别的颜色),
        #    半透明一律走 setAlpha
        fill = QColor(T.ACCENT)
        fill.setAlpha(43)
        painter.setBrush(fill)                          # 半透明蓝底
        painter.drawRect(r)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(T.ACCENT), 1))       # 蓝色实线边框
        painter.drawRect(r.adjusted(0, 0, -1, -1))
        painter.end()

    def _select_in_rect(self, content_rect: QRect):
        """按"内容坐标"矩形选照片(visualRect 是视口坐标, 先换算过去)。

        以前矩形按屏幕坐标记: 滚动页面后内容在动、矩形不动, 紧接着一次鼠标移动
        就会用 ClearAndSelect 把"现在位于矩形下的"换上去, 之前框选的全被冲掉。
        """
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        m = self.photo_model()
        if m is None:
            return
        vp_rect = self._to_viewport(content_rect)
        sel = QItemSelection()
        for i in range(len(m.rows)):
            if not self._is_photo(i):
                continue
            vr = self.visualRect(m.index(i))
            if vr.intersects(vp_rect):
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
        # 合并/统一模式: 跨组拖拽没有意义(合不到一起 / 统一规格不同), 直接不响应
        # v217: 以前只判 merge_mode。统一态 group_key_of 恒返回 None → 判定永远放行,
        #       把一个 h265 拖进 h264 段会出现重复的编码组标题。统一态改用 norm_key。
        if 0 <= target_row < len(m.rows):
            src_key = self._group_lock_key(m, m.rows[cur[0]].photo)
            dst = m.rows[target_row]
            if isinstance(dst, PhotoRow) and src_key is not None:
                if self._group_lock_key(m, dst.photo) != src_key:
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

    def _group_lock_key(self, m, photo):
        """拖拽时用来判"跨组不许拖"的组标识。

        合并态 = 规格组 key; 统一态 = norm_key(编码→分辨率→帧率 的截断元组);
        排序态 = None(随便拖, 拖完会写 group_override 跟着落点那组走)。
        """
        if self.merge_mode:
            return m.group_key_of(photo)
        if getattr(m, "norm_groups", None) is not None:
            return m.norm_key_of(photo)
        return None

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
        if self._rubber_on:
            # 拉框中滚轮: 让框长到新滚出来的那一截。
            # (滚动条 valueChanged 也会走一次, 这里再兜一次底 —— 万一某条滚动
            #  路径没触发 valueChanged, 框也一定跟着滚轮走。)
            self._rubber_after_scroll()

    def keyPressEvent(self, event):
        if self._live and event.key() == Qt.Key_Escape:
            self._end_live(False)
            return
        if event.key() == Qt.Key_Escape:
            # 排序 / 合并两种模式下都能用 ESC 一次性取消全部选中
            if self.selectionModel() is not None and self.selectionModel().hasSelection():
                self.selectionModel().clearSelection()
                self.viewport().update()
                event.accept()
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
        # 必须按"行号"排序: selectedIndexes() 返回的是选中模型里的内部顺序
        # (基本等于点击先后), 直接拿它合并会导致后点的排在前面。
        # 按行号 = 网格里从上到下、从左到右的先后, 也就是主界面的排序。
        idxs = sorted(self.selectedIndexes(), key=lambda i: i.row())
        return m.selected_photos(idxs)

    def selectionCount(self) -> int:
        return sum(1 for i in self.selectedIndexes() if self._is_photo(i.row()))

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QEvent.Wheel:
            self._thumb_timer.start()
        return super().eventFilter(obj, event)
