# -*- coding: utf-8 -*-
"""剪辑态的素材框(左上): 缩略图网格。

工作流(v117 用户定):
- 进剪辑时文件先全部落在素材框, **时间线保持空**;
- 只有点格子**右下角的 ＋** 才会加入时间线(落点=播放头);
- 点格子其他地方只是选中, 选中后可右键出菜单;
- 拖到轨道上也可以(保留, 剪映也是这么用)。

铁律(预览列表踩过四次的坑):
- 不写 `QListWidget::item` 的 QSS, 那样 Qt 会走样式表项渲染, 吃掉一切自绘;
- 每一项先 fillRect 画不透明底, 再画高亮, 绝不用 setBackground 去"清除"
  (无效 QColor 带主题会渲染成黑条)。
"""
from __future__ import annotations

from PySide6.QtCore import (QMimeData, QItemSelectionModel, QPoint, QRectF,
                           QSize, Qt, Signal)
from PySide6.QtGui import QColor, QDrag, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QStyledItemDelegate, QStyle, QVBoxLayout,
                               QWidget)

from . import theme as T
from .edit_icons import make_icon

ROLE_PATH = Qt.UserRole + 1
ROLE_KIND = Qt.UserRole + 2
ROLE_USED = Qt.UserRole + 3

CELL_W = 104
CELL_H = 100
THUMB_H = 54
PLUS_D = 20                     # 右下角 ＋ 的直径

# 素材网格最后面那个「＋ 添加素材」占位格(和素材格一样大)
ADD_MARKER = "\x00add-media"


def is_add_cell(item) -> bool:
    return item is not None and (item.data(ROLE_PATH) or "") == ADD_MARKER


def plus_rect(cell) -> QRectF:
    """＋ 的热区(与绘制用同一套几何, 免得点偏)。贴在缩略图的右下角。"""
    r = QRectF(cell)
    return QRectF(r.right() - PLUS_D - 6, r.top() + 8 + THUMB_H - PLUS_D,
                  PLUS_D, PLUS_D)


class _MediaDelegate(QStyledItemDelegate):
    """自绘: 缩略图 + 文件名 + 右下角 ＋; 已上轨的整项淡化并标「已用」。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._list = parent

    def sizeHint(self, option, index):
        return QSize(CELL_W, CELL_H)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = option.rect

        # 1) 不透明底(不吃系统/主题的背景色)
        painter.fillRect(rect, QColor(T.SURFACE))

        # 1.5) 最后一个格子是「＋ 添加素材」占位块(和素材格一样大)
        if (index.data(ROLE_PATH) or "") == ADD_MARKER:
            self._paint_add_cell(painter, option, QRectF(rect))
            painter.restore()
            return

        # 2) 高亮: 选中深、悬浮浅(自己画, 不碰 BackgroundRole)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#C2D6FB" if selected else T.ACCENT_SOFT))
            painter.drawRoundedRect(QRectF(rect).adjusted(2, 2, -2, -2), 6, 6)

        used = bool(index.data(ROLE_USED))

        # 3) 缩略图(占位块用明显的浅灰, 别用几乎透明的底)
        tr = QRectF(rect.x() + 8, rect.y() + 7, rect.width() - 16, THUMB_H)
        pm = index.data(Qt.DecorationRole)
        if pm is not None and not pm.isNull():
            scaled = pm.scaled(int(tr.width()), int(tr.height()),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap(int(tr.center().x() - scaled.width() / 2),
                               int(tr.center().y() - scaled.height() / 2),
                               scaled)
        else:
            painter.setPen(QPen(QColor(T.BORDER), 1))
            painter.setBrush(QColor("#ECECEC"))
            painter.drawRoundedRect(tr, 4, 4)
            painter.setPen(QColor(T.TEXT_SUB if not used else T.TEXT_WEAK))
            f = QFont()
            f.setPointSize(9)
            painter.setFont(f)
            painter.drawText(tr, Qt.AlignCenter, index.data(ROLE_KIND) or "")

        # 4) 文件名(单行截断, 已用的用弱色区分)
        f = QFont()
        f.setPointSize(9)
        painter.setFont(f)
        fm = QFontMetrics(f)
        name = index.data(Qt.DisplayRole) or ""
        text_rect = QRectF(rect.x() + 4, rect.y() + THUMB_H + 12,
                           rect.width() - 8, 18)
        elided = fm.elidedText(name, Qt.ElideMiddle, int(text_rect.width()))
        painter.setPen(QColor(T.TEXT_WEAK if used else T.TEXT))
        painter.drawText(text_rect, Qt.AlignCenter, elided)

        # 5) 右下角 ＋(hover 时更醒目, 这是"加入时间线"的唯一入口)
        pr = plus_rect(rect)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(T.ACCENT if hovered or selected else "#4C7DF0"))
        painter.drawEllipse(pr)
        painter.setPen(QPen(QColor("#FFFFFF"), 1.8))
        painter.drawLine(int(pr.center().x() - 4), int(pr.center().y()),
                         int(pr.center().x() + 4), int(pr.center().y()))
        painter.drawLine(int(pr.center().x()), int(pr.center().y() - 4),
                         int(pr.center().x()), int(pr.center().y() + 4))

        # 6) 已上轨: 右上角「已用」(最后画, 不跟着淡化)
        if used:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(T.ACCENT))
            tag = QRectF(rect.right() - 36, rect.y() + 6, 30, 16)
            painter.drawRoundedRect(tag, 4, 4)
            painter.setPen(QColor("#FFFFFF"))
            f2 = QFont()
            f2.setPointSize(8)
            painter.setFont(f2)
            painter.drawText(tag, Qt.AlignCenter, "已用")
        painter.restore()

    @staticmethod
    def _paint_add_cell(painter, option, rect: QRectF):
        """占位块: 虚线框 + 大加号 + 「添加素材」。点它 = 打开添加素材。"""
        hovered = bool(option.state & QStyle.State_MouseOver)
        box = rect.adjusted(4, 4, -4, -4)
        pen = QPen(QColor(T.ACCENT if hovered else T.BORDER_STRONG), 1.4,
                   Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor(T.ACCENT_SOFT if hovered else "transparent"))
        painter.drawRoundedRect(box, 8, 8)

        cx, cy = box.center().x(), box.center().y() - 8
        color = QColor(T.ACCENT if hovered else T.TEXT_SUB)
        painter.setPen(QPen(color, 2))
        painter.drawLine(int(cx - 9), int(cy), int(cx + 9), int(cy))
        painter.drawLine(int(cx), int(cy - 9), int(cx), int(cy + 9))

        painter.setPen(color)
        f = QFont()
        f.setPointSize(9)
        painter.setFont(f)
        painter.drawText(QRectF(box.left(), cy + 16, box.width(), 20),
                         Qt.AlignCenter, "添加素材")


class _MediaList(QListWidget):
    """点 ＋ = 加入时间线; 点别处 = 选中(Ctrl 可多选); 右键 = 菜单; 拖出去 = 落到轨道。

    Delete = 把选中的移出素材框(交给主窗口, 磁盘文件不动)。
    """

    plusClicked = Signal(str)
    contextRequested = Signal(str, QPoint)
    addRequested = Signal()
    deleteRequested = Signal(list)      # 按 Delete: 选中的全部路径
    picked = Signal(str)                # 点中(选中)一个素材 = 中间预览显示它
    blank = Signal()                    # 点空白处 = 预览回到裁剪线

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            paths = self._selected_paths()
            if paths:
                self.deleteRequested.emit(paths)
                return
        super().keyPressEvent(event)

    def _selected_paths(self) -> list:
        out = []
        for item in self.selectedItems():
            p = item.data(ROLE_PATH) or ""
            if p and p != ADD_MARKER:
                out.append(p)
        return out

    def _plus_hit(self, item, pos) -> bool:
        if item is None or is_add_cell(item):
            return False
        return plus_rect(self.visualItemRect(item)).contains(
            float(pos.x()), float(pos.y()))

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item is None:
            if event.button() == Qt.RightButton:
                # v177: 框选后右键空白处 —— 清**空选中会丢掉刚框选的一批**,
                # 所以只在"还选着东西"时给整批菜单, 绝不调用 super(那会清空选择)。
                sel = self.selectedItems()
                if sel:
                    first = sel[0].data(ROLE_PATH) or ""
                    if first:
                        self.contextRequested.emit(
                            first, event.globalPosition().toPoint())
                return
            # 点空白处: 交给 QListWidget 清选中, 再让预览回到裁剪线
            super().mousePressEvent(event)
            if event.button() == Qt.LeftButton:
                self.blank.emit()
            return
        if item is not None:
            # 占位格: 点哪儿都是「添加素材」
            if is_add_cell(item):
                if event.button() == Qt.LeftButton:
                    self.addRequested.emit()
                return
            if event.button() == Qt.LeftButton and self._plus_hit(item, pos):
                path = item.data(ROLE_PATH) or ""
                if path:
                    self.plusClicked.emit(path)
                return
            if event.button() == Qt.RightButton:
                path = item.data(ROLE_PATH) or ""
                if item.isSelected():
                    # 已在选中集合里: 保持整批选中, 只把 current 指过来。
                    # ⚠️ 不能用 setCurrentItem(): QListWidget 的 setCurrentIndex
                    # 内部走的是 ClearAndSelect —— 一调就把框选的那批全部清掉,
                    # 只剩当前这一个(v179 修)。这里直接用 selectionModel
                    # 只改 current, 不碰选择集。
                    try:
                        idx = self.indexFromItem(item)
                        self.selectionModel().setCurrentIndex(
                            idx, QItemSelectionModel.NoUpdate)
                    except Exception:
                        pass
                else:
                    # 没选中它: 按常规选中它(会替换整批), 然后出菜单
                    self.setCurrentItem(item)
                    item.setSelected(True)
                if path:
                    self.contextRequested.emit(path,
                                               event.globalPosition().toPoint())
                return
            if event.button() == Qt.LeftButton:
                path = item.data(ROLE_PATH) or ""
                super().mousePressEvent(event)
                if path:
                    self.picked.emit(path)   # 只选中, 不自动上轨
                return
        super().mousePressEvent(event)

    def startDrag(self, supportedActions):
        item = self.currentItem()
        if item is None or is_add_cell(item):
            return
        path = item.data(ROLE_PATH) or ""
        if not path:
            return
        md = QMimeData()
        md.setText(path)
        drag = QDrag(self)
        drag.setMimeData(md)
        pm = item.data(Qt.DecorationRole)
        if pm is not None and not pm.isNull():
            drag.setPixmap(pm.scaled(72, THUMB_H, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation))
        drag.exec(Qt.CopyAction)


class MediaPanel(QWidget):
    """素材网格 + 添加按钮。"""

    addRequested = Signal()             # 点「添加素材」
    plusClicked = Signal(str)           # 点格子右下角 ＋ = 加入时间线
    contextRequested = Signal(str, QPoint)   # 右键某个素材
    deleteRequested = Signal(list)      # Ctrl 选中后按 Delete
    picked = Signal(str)                # 点中某格(没点 ＋) = 预览它
    blankClicked = Signal()             # 点空白 = 预览回裁剪线
    playRequested = Signal()            # 双击某格 = 播它

    def __init__(self, parent=None):
        super().__init__(parent)
        self._missing: list = []       # 还没拿到缩略图的路径(交给主窗口去补)
        self._quiet = False            # 程序自己改选中(刷新素材框)时别当成"用户点了"
        self._build()

    def _build(self):
        # 四周不留白: 这一块本来就不大, 白边纯属浪费。
        # 标题「素材」和「添加素材」按钮都撤了 —— 识别靠内容, 添加靠末尾的占位格。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.list = _MediaList()
        self.list.setObjectName("mediaList")
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setSpacing(4)
        self.list.setGridSize(QSize(CELL_W, CELL_H))
        # Ctrl 点选多个(一起删)、Shift 连选一段
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setMouseTracking(True)
        self.list.setDragEnabled(True)
        self.list.setAcceptDrops(False)
        self.list.setStyleSheet(
            f"QListWidget#mediaList {{ background: {T.SURFACE};"
            f" border: 1px solid {T.BORDER}; border-radius: 8px; }}")
        self.list.setItemDelegate(_MediaDelegate(self.list))
        self.list.plusClicked.connect(self.plusClicked.emit)
        self.list.contextRequested.connect(self.contextRequested.emit)
        self.list.addRequested.connect(self.addRequested.emit)
        self.list.deleteRequested.connect(self.deleteRequested.emit)
        self.list.picked.connect(self._on_picked)
        self.list.blank.connect(self.blankClicked.emit)
        # 双击 = 直接播放这个素材
        self.list.itemDoubleClicked.connect(self._on_double)
        self.list.setToolTip("点格子选中（按住 Ctrl 可选多个，Delete 移出素材框）；"
                             "点右下角 ＋ 加入时间线")
        root.addWidget(self.list, 1)

    def _on_picked(self, path: str):
        """用户点了某一格(没点 ＋): 让中间预览显示它。"""
        if not self._quiet and path:
            self.picked.emit(path)

    def _on_double(self, item):
        path = item.data(ROLE_PATH) if item is not None else ""
        if path and path != ADD_MARKER:
            self.picked.emit(path)
            self.playRequested.emit()

    # ---------------------------------------------------------------- 数据
    def set_sources(self, rows):
        """rows: [(path, kind_label, used, pixmap|None), ...]

        原地更新: 按路径复用条目, 只改「已用」和缩略图。
        整表 clear()+重建会丢掉选中项、滚动位置, 还会让正在拖的条目变成野指针。
        """
        cur = self.list.currentItem()
        cur_path = cur.data(ROLE_PATH) if cur is not None else None

        by_path: dict[str, QListWidgetItem] = {}
        for i in range(self.list.count()):
            it = self.list.item(i)
            by_path[it.data(ROLE_PATH)] = it

        wanted = {row[0] for row in rows}
        # 1) 先删掉不再需要的(倒序删, 下标不乱); 末尾的「添加素材」占位格永远留着
        for i in reversed(range(self.list.count())):
            it = self.list.item(i)
            path = it.data(ROLE_PATH)
            if path == ADD_MARKER:
                continue
            if path not in wanted:
                self.list.takeItem(i)
                by_path.pop(path, None)

        # 2) 按 rows 的顺序补/移动/更新
        missing: list = []
        for idx, (path, kind, used, pm) in enumerate(rows):
            it = by_path.get(path)
            if it is None:
                it = QListWidgetItem()
                it.setData(ROLE_PATH, path)
                self.list.insertItem(idx, it)
                by_path[path] = it
            elif self.list.row(it) != idx:
                self.list.takeItem(self.list.row(it))
                self.list.insertItem(idx, it)
            it.setText(path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1])
            it.setData(ROLE_KIND, kind)
            it.setData(ROLE_USED, bool(used))
            if pm is not None and not pm.isNull():
                it.setData(Qt.DecorationRole, pm)
            elif self._icon_of(it) is None:
                missing.append(path)      # 缩略图还没生成: 记下来让主窗口去补
            it.setToolTip(path)
            it.setSizeHint(QSize(CELL_W, CELL_H))
        self._missing = missing

        # 3) 占位格永远排在最后一个
        add_item = by_path.get(ADD_MARKER)
        if add_item is None:
            add_item = QListWidgetItem()
            add_item.setData(ROLE_PATH, ADD_MARKER)
            add_item.setFlags(Qt.ItemIsEnabled)
            self.list.addItem(add_item)
        add_item.setText("")
        add_item.setToolTip("添加素材（视频 / 图片 / 音频）")
        add_item.setSizeHint(QSize(CELL_W, CELL_H))
        last = self.list.count() - 1
        if self.list.row(add_item) != last:
            self.list.takeItem(self.list.row(add_item))
            self.list.addItem(add_item)

        # 4) 选中项尽量保住
        if cur_path:
            for i in range(self.list.count()):
                if self.list.item(i).data(ROLE_PATH) == cur_path:
                    self.list.setCurrentRow(i)
                    break

    def path_of(self, item) -> str:
        return item.data(ROLE_PATH) or ""

    def first_path(self) -> str:
        """素材框里的第一个素材(没有就空串)。

        主窗口用它填预览: 时间线还是空的时候, 中间那块不该是一块灰白的空框,
        应该直接把素材框第一个素材的画面显示出来(v151)。

        ⚠️ 必须跳过末尾那个「＋ 添加素材」占位格(v164): 它的"路径"是 ADD_MARKER,
        当素材全被移走后它会成为唯一的条目, 直接返回它等于"告诉他还有素材" ——
        于是该清空的预览一直挂着上一张图。
        """
        for i in range(self.list.count()):
            p = self.path_of(self.list.item(i))
            if p and p != ADD_MARKER:
                return p
        return ""

    def selected_path(self) -> str:
        item = self.list.currentItem()
        return self.path_of(item) if item is not None else ""

    def selected_paths(self) -> list:
        """Ctrl 多选后选中的全部路径(不含末尾的「添加素材」占位格)。"""
        return self.list._selected_paths()

    def select_all(self) -> int:
        """Ctrl+A(焦点在素材框): 全选素材。

        ⚠️ 不能直接用 QListWidget.selectAll(): 末尾那个「＋ 添加素材」是普通格子,
        会被一起选中(看着像"素材被选中了还多了个蓝块"), 所以自己跳过它。
        """
        n = 0
        try:
            self.list.clearSelection()
            for i in range(self.list.count()):
                it = self.list.item(i)
                if it is None or is_add_cell(it):
                    continue
                it.setSelected(True)
                n += 1
        except Exception:
            pass
        return n

    def clear_selection(self):
        """程序自己把素材框的选择清掉(选中了右侧片段时用, 见 v145 的互斥)。

        ⚠️ 要走 _quiet: 否则清选中会被当成"用户点了空白", 把预览又切回裁剪线。
        """
        self._quiet = True
        try:
            self.list.clearSelection()
            self.list.setCurrentItem(None)
        finally:
            self._quiet = False

    # ---------------------------------------------------------------- 缩略图
    @staticmethod
    def _icon_of(item):
        pm = item.data(Qt.DecorationRole)
        if pm is None:
            return None
        try:
            if pm.isNull():
                return None
        except Exception:
            return None
        return pm

    def missing_thumbs(self) -> list:
        """哪些素材还没有缩略图(主窗口拿到后去补, 格子才有预览)。"""
        return list(self._missing)

    def set_thumb(self, path: str, pm) -> bool:
        """缩略图补回来了: 只改那一项, 不重建整表(重建会丢掉多选)。"""
        if not path or pm is None:
            return False
        try:
            if pm.isNull():
                return False
        except Exception:
            return False
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(ROLE_PATH) == path:
                it.setData(Qt.DecorationRole, pm)
                if path in self._missing:
                    self._missing.remove(path)
                self.list.viewport().update()
                return True
        return False

    def count(self) -> int:
        return self.list.count()
