# -*- coding: utf-8 -*-
"""右侧信息面板: 时间优先 + 全量元数据(可编辑)。"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDateTimeEdit, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
                               QSizePolicy, QStyledItemDelegate, QStyle, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from . import theme as T
from ..core import fields as F
from ..core.photo import Photo, STATUS_PENDING
from .widgets import CheckCheckBox
from .dialogs import _DateTimeSegments


class _ClickableLabel(QLabel):
    """可点击的 QLabel（用于预览图：单击 → 用系统默认看图软件打开）。"""

    clicked = Signal()
    resized = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class _InfoIcon(QLabel):
    """小叹号提示图标: 圆底 + 白色感叹号, 悬浮才显示 toolTip。"""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(text)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        d = self.width()
        r = d / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(T.ACCENT))
        p.drawEllipse(0, 0, d, d)
        p.setPen(QPen(QColor("#FFFFFF"), 1.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(int(r), int(d * 0.30), int(r), int(d * 0.57))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(int(r) - 1, int(d * 0.66), 2, 2)
        super().paintEvent(event)


class _TimeSegField(QWidget):
    """一行时间字段: 文字标签 + _DateTimeSegments (6 段) + 撤销按钮, 全部同一行。
    与 FieldRow 保持相同外部接口 (value / set_value / btn / _original / _dirty / edit /
    changed / reverted), 让 _on_time_edited 等上层逻辑几乎不用改。
    """

    changed = Signal(object)
    reverted = Signal(str)

    def __init__(self, key: str, label_text: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._original_dt = None
        self._dirty = False

        # 行内上下不留边距, 三行之间的疏密完全交给外层容器的 spacing 控制(更紧凑)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        cap = QLabel(label_text)
        cap.setProperty("muted", True)
        cap.setMinimumWidth(60)
        lay.addWidget(cap)

        self.segments = _DateTimeSegments(datetime(2000, 1, 1, 0, 0, 0))
        self.segments.changed.connect(self._on_changed)
        lay.addWidget(self.segments, 1)

        self.edit = self.segments

        # 撤销按钮: 固定宽度的"槽位"始终占着位置, 按钮只在改动后出现,
        # 这样按钮显隐时时间输入框不会被挤窄/撑宽。
        self.btn = QPushButton("\u2715")          # ✕
        self.btn.setObjectName("timeRevert")
        self.btn.setFixedSize(20, 20)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setToolTip("撤销这一项改动")
        self.btn.setStyleSheet(
            f"#timeRevert {{ border: none; background: transparent; padding: 0px;"
            f" min-height: 0px; font-size: 13px; font-weight: 600;"
            f" border-radius: 10px; color: {T.TEXT_WEAK}; }}"
            f"#timeRevert:hover {{ background: {T.DANGER_SOFT}; color: {T.DANGER}; }}")
        self.btn.setVisible(False)
        self.btn.clicked.connect(lambda: self.reverted.emit(self.key))
        self._btn_hold = QWidget()
        self._btn_hold.setFixedWidth(20)
        hold = QHBoxLayout(self._btn_hold)
        hold.setContentsMargins(0, 0, 0, 0)
        hold.setSpacing(0)
        hold.addWidget(self.btn, 0, Qt.AlignCenter)
        lay.addWidget(self._btn_hold, 0, Qt.AlignVCenter)

    def _on_changed(self):
        cur = self.segments.datetime()
        dirty = (self._original_dt is not None and cur != self._original_dt)
        if dirty != self._dirty:
            self._dirty = dirty
            self.btn.setVisible(dirty)
            self._apply_dirty_color(dirty)
        self.changed.emit(None)

    def _apply_dirty_color(self, dirty):
        self.segments.setStyleSheet(f"color: {T.ACCENT};" if dirty else "")

    def value(self):
        return self.segments.datetime().strftime("%Y-%m-%d %H:%M:%S")

    def set_value(self, text, original, pending):
        def _to_dt(s):
            s = (s or "").strip()
            if not s:
                return None
            try:
                return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        cur_dt = _to_dt(text) or datetime(2000, 1, 1, 0, 0, 0)
        self._original_dt = _to_dt(original)
        # 同时保留字符串版 _original, 兼容老接口 (Inspector._on_time_edited 会直接赋值)
        self._original = original if original else ""
        self.segments.blockSignals(True)
        for sp, v in zip(self.segments._spins,
                         (cur_dt.year, cur_dt.month, cur_dt.day,
                          cur_dt.hour, cur_dt.minute, cur_dt.second)):
            sp.setValue(v)
        self.segments.blockSignals(False)
        self._dirty = bool(pending)
        self.btn.setVisible(self._dirty)
        self._apply_dirty_color(self._dirty)
        self.setEnabled(self._original_dt is not None or bool(text))


class _MetaEditDelegate(QStyledItemDelegate):
    """元数据树双击编辑: 只允许改"值"那一列, 且只有可写字段才开编辑器。

    - 字段名(第 0 列)是标签, 永远不能改; 双击它只做选中, 不弹输入框。
    - 只读字段(尺寸/文件大小/修订历史等)即便被点了也不弹编辑器。
    - 编辑器的几何要撑满整行高度: 默认实现把它限制在单元格内边距后的
      "内容区", 行高偏矮时输入框只有 ~17px 高, 文字被上下裁切。
    """

    ROLE_WRITABLE = Qt.UserRole + 1

    def createEditor(self, parent, option, index):
        if index.column() != 1:
            return None
        if not bool(index.data(self.ROLE_WRITABLE)):
            return None
        return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index):
        # v198: 树里显示的是「去掉时区」的值, 但编辑必须用原始值 ——
        # 否则用户只是双击确认一下, 写回 pending 的就是少了 +08:00 的显示值,
        # 会破坏「XMP/QuickTime/文件系统按带时区写入」的既定规则。
        # 原值存在列 1 的 UserRole(见 _rebuild_tree), 显示值在 Display/EditRole。
        raw = index.data(Qt.UserRole)
        if raw is not None and hasattr(editor, "setText"):
            editor.setText(str(raw))
        else:
            super().setEditorData(editor, index)

    def updateEditorGeometry(self, editor, option, index):
        r = option.rect
        h = max(r.height(), editor.sizeHint().height())
        y = r.y() - (h - r.height()) // 2
        editor.setGeometry(r.x(), y, r.width(), h)


class Inspector(QWidget):
    """右侧面板。多选时值显示为混合, 编辑会应用到全部选中项。"""

    planChanged = Signal()
    editsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.photos: list[Photo] = []
        self._rows = {}
        self._loading = False
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 预览块：占满整行宽 + 一大段高度；图片按比例「完整显示」(contain) 在这块面积里，
        # 取最大尺寸但不裁切。直接是 outer 的第一个控件，上/左/右都顶到面板边缘。
        self.preview = _ClickableLabel()
        self.preview.setMinimumHeight(240)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setCursor(Qt.PointingHandCursor)
        self.preview.setToolTip("单击用系统默认看图软件打开")
        # 预览块自身的浅灰背景 = 图片之外的留白区域(图片完整显示, 不被裁切)
        self.preview.setStyleSheet(
            f"background: {T.THUMB_BG}; color: {T.TEXT_WEAK};")
        self.preview.clicked.connect(self._open_current)
        # 预览块自身尺寸变化(窗口缩放/分栏拖动)时按新尺寸重绘
        self.preview.resized.connect(self._render_preview)
        self._preview_src = None
        outer.addWidget(self.preview, 4)

        # 下方内容容器：统一左/右内缩 12，避免文字贴边（预览块已顶边，此处单独缩进）
        # 注意: 这里**不给 stretch**——它只占自己实际需要的高度(文件名+时间),
        # 多出来的竖直空间全部让给下面的元数据树, 避免内部被拉开留白。
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(12, 8, 12, 8)
        cl.setSpacing(8)
        outer.addWidget(content, 0)

        # 文件名(占满左侧弹性空间) + 右侧「大小」紧挨「在资源管理器显示位置」按钮
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(6)
        self.name_label = QLabel("未选择")
        self.name_label.setProperty("h2", True)
        self.name_label.setWordWrap(True)
        name_row.addWidget(self.name_label, 1)
        self.size_label = QLabel("")
        self.size_label.setProperty("weak", True)
        self.size_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        name_row.addWidget(self.size_label, 0, Qt.AlignTop)
        self.reveal_btn = QPushButton()
        self.reveal_btn.setObjectName("revealBtn")
        self.reveal_btn.setFixedSize(26, 26)
        self.reveal_btn.setCursor(Qt.PointingHandCursor)
        self.reveal_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.reveal_btn.setToolTip("在资源管理器中显示该文件的位置")
        self.reveal_btn.clicked.connect(self._reveal_current)
        self.reveal_btn.setVisible(False)
        name_row.addWidget(self.reveal_btn, 0, Qt.AlignTop)
        cl.addLayout(name_row)

        cl.addWidget(self._divider())

        # 时间区
        head = QHBoxLayout()
        title = QLabel("时间")
        title.setProperty("h2", True)
        head.addWidget(title)
        head.addSpacing(4)
        # 小叹号图标: 悬浮才显示提示文字, 置于"时间"右上
        self.info_icon = _InfoIcon("安卓相册按这三个时间排序，排序结果以这里的值为准")
        head.addWidget(self.info_icon, 0, Qt.AlignTop)
        head.addStretch(1)
        self.link_check = CheckCheckBox("联动")
        self.link_check.setChecked(True)
        self.link_check.setToolTip("开启后, 改任意一个时间会同时改另外两个")
        head.addWidget(self.link_check)
        cl.addLayout(head)

        self.time_rows = {}
        time_box = QVBoxLayout()
        time_box.setContentsMargins(0, 0, 0, 0)
        time_box.setSpacing(3)      # 三行时间更紧凑
        for key in (F.MODIFIED, F.CREATED, F.TAKEN):
            field = _TimeSegField(key, F.SYNTHETIC_LABEL[key])
            field.changed.connect(lambda _p, k=key: self._on_time_edited(k))
            field.reverted.connect(self._on_time_reverted)
            self._rows[key] = field  # 复用 _rows 字典, 兼容 set_photos / _on_time_edited
            self.time_rows[key] = field
            time_box.addWidget(field)
        cl.addLayout(time_box)

        self.apply_all_btn = QPushButton("把当前时间应用到全部选中")
        self.apply_all_btn.setObjectName("applyAll")
        self.apply_all_btn.setVisible(False)
        cl.addWidget(self.apply_all_btn)

        cl.addWidget(self._divider())

        # 元数据标题行（含搜索框 + 仅可写）：与下方 tree 固定在一起
        head2 = QHBoxLayout()
        head2.setContentsMargins(12, 2, 12, 6)
        head2.setSpacing(8)
        t2 = QLabel("元数据")
        t2.setProperty("h2", True)
        head2.addWidget(t2)

        # 「搜索字段名或值」输入框: 放在「元数据」标题与「仅可写」之间, 占满中间剩余宽度
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("搜索字段名或值…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMinimumWidth(110)
        self.filter_edit.setFixedHeight(26)
        self.filter_edit.textChanged.connect(lambda _=0: self.refresh())
        head2.addWidget(self.filter_edit, 1)

        self.editable_only = CheckCheckBox("仅可写")
        self.editable_only.setChecked(True)
        self.editable_only.setToolTip("隐藏只读字段(Composite / System 等)")
        self.editable_only.stateChanged.connect(lambda _=0: self.refresh())
        head2.addWidget(self.editable_only)
        outer.addLayout(head2)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setIndentation(12)
        self.tree.setFrameShape(QTreeWidget.NoFrame)
        self.tree.setVerticalScrollMode(QTreeWidget.ScrollPerPixel)
        # 选中以"整行"为单位: 配合主题里 QTreeView::item:selected 的背景色,
        # 选中时整行(含缩进区)都铺上浅蓝底, 而不是只在最左边露出一小块。
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setAllColumnsShowFocus(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, header.ResizeMode.Stretch)
        self.tree.itemChanged.connect(self._on_item_changed)
        # 自定义编辑委托: 双击编辑时让输入框撑满整行高度。
        # 默认的 QStyledItemDelegate 会把编辑器限制在单元格内边距后的"内容区",
        # 行高偏矮时输入框只有 17px 高、文字被上下裁切 → 显示不完整。
        self.tree.setItemDelegate(_MetaEditDelegate())

        # 元数据树吃掉预览块之外的全部剩余高度(自带垂直滚动条)。
        # 与预览块同为 stretch 4 → 两者平分富余空间, 内容区不再抢高度。
        self.tree.setMinimumHeight(120)
        outer.addWidget(self.tree, 4)

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {T.BORDER}; background: {T.BORDER};")
        line.setFixedHeight(1)
        return line

    # ---------------------------------------------------------------- 打开 / 定位
    def _current(self):
        """当前预览的那张照片（多选时取选中集里的第一张，未选为 None）。

        多选时预览也显示第一张，所以"单击预览 / 定位文件"自然跟着这同一张，
        与合并模式右栏的 _photos[0] 口径一致。
        """
        return self.photos[0] if self.photos else None

    def _open_current(self):
        """单击预览图：用系统默认看图软件打开当前文件。"""
        p = self._current()
        if p is None or not os.path.exists(p.path):
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(p.path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p.path])
            else:
                subprocess.Popen(["xdg-open", p.path])
        except Exception as ex:
            QMessageBox.warning(self, "打开失败", f"无法用默认应用打开:\n{p.path}\n\n{ex}")

    def _reveal_current(self):
        """文件名右侧图标按钮：在资源管理器中定位该文件。"""
        p = self._current()
        if p is None:
            return
        path = os.path.normpath(p.path)
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass

    def set_photos(self, photos: list[Photo]):
        self._loading = True
        self.photos = list(photos)
        self._preview_src = None
        self.preview.setText("")
        self.reveal_btn.setVisible(False)
        # v216: 只有"点了真有用"(单选一个文件)时, 预览图才算可点区域; 多选/空的时候
        # 点它什么也不会发生 → 明说不可点(全局光标规则据此给箭头, 别装成能点)
        self.preview.setProperty("handCursor", len(self.photos) == 1)
        self.preview.setCursor(Qt.ArrowCursor)
        if len(self.photos) == 1:
            photo = self.photos[0]
            self.reveal_btn.setVisible(True)
            self.preview.setCursor(Qt.PointingHandCursor)
            # 只显示文件名称 + 占用空间, 不加冒号, 不显示路径/扩展名/类型
            self.name_label.setText(photo.name)
            self.size_label.setText(self._human_size(photo.size))
            if photo.thumb and not photo.thumb.isNull():
                self._preview_src = photo.thumb
                self._render_preview()
            else:
                self.preview.setText("缩略图加载中…")
        elif len(self.photos) > 1:
            total = sum(p.size for p in self.photos)
            self.name_label.setText(f"已选中 {len(self.photos)} 张")
            # 只显示张数 + 合计大小, 不显示路径/名称列表
            self.size_label.setText(f"合计 {self._human_size(total)}")
            # v191: 和合并模式一样，多选也显示"框选/多选里的第一张"的预览，
            # 不再是空底上一行「N 张」。缩略图还没解码完时先给占位文字，
            # 后台解码完成会由主窗口 _on_thumb_ready 再调一次 set_photos 回填。
            self.preview.setCursor(Qt.PointingHandCursor)
            first = self.photos[0]
            if first.thumb and not first.thumb.isNull():
                self._preview_src = first.thumb
                self._render_preview()
            else:
                self.preview.setText("缩略图加载中…")
        else:
            self.name_label.setText("未选择")
            self.size_label.setText("在左侧点选或框选照片")
            self.preview.setText("无预览")
        self._loading = False
        self.refresh()

    def _render_preview(self):
        """完整显示整张照片: 按比例缩放到预览块内(contain), 尽量占满但绝不裁切。
        预览块自身的背景色会填满图片之外的区域。"""
        src = getattr(self, "_preview_src", None)
        if src is None or src.isNull():
            return
        bw = self.preview.width()
        bh = self.preview.height()
        if bw <= 1 or bh <= 1:
            return
        self.preview.setPixmap(src.scaled(bw, bh, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 预览块尺寸变化时按新尺寸重绘(图片始终铺满)
        self._render_preview()

    @staticmethod
    def _human_size(size: int) -> str:
        if size <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB"]
        i = 0
        val = float(size)
        while val >= 1024 and i < len(units) - 1:
            val /= 1024
            i += 1
        return f"{val:.1f} {units[i]}" if i else f"{int(val)} B"

    def refresh(self):
        if self._loading:
            return
        photos = self.photos
        for key, wrap in self.time_rows.items():
            row = self._rows[key]
            if not photos:
                row.set_value("", None, False)
                continue
            values = {p.synthetic_display(key) for p in photos}
            if len(values) == 1:
                text = values.pop()
            else:
                text = "（多个不同值）"
            planned = any(key in p.planned for p in photos)
            row.set_value(text, "" if not photos or len(photos) > 1 else
                          (photos[0].raw(key) or ""), planned)
        self.apply_all_btn.setVisible(len(photos) > 1)
        self._fill_tree()

    # ---------------------------------------------------------------- 时间编辑
    def _on_time_edited(self, key: str):
        if self._loading or not self.photos:
            return
        row = self._rows[key]
        text = row.value()
        dt = self._parse_loose(text)
        if dt is None:
            return
        targets = (F.MODIFIED, F.CREATED, F.TAKEN) if self.link_check.isChecked() else (key,)
        for photo in self.photos:
            for t in targets:
                photo.planned[t] = dt
            photo.status = STATUS_PENDING
        for t in targets:
            if t != key:
                self._rows[t].set_value(dt.strftime("%Y-%m-%d %H:%M:%S"),
                                        self._rows[t]._original, True)
        row._original = text
        row._dirty = True
        row.btn.setVisible(True)
        row.edit.setStyleSheet(f"color: {T.ACCENT};")
        self.planChanged.emit()

    def _on_time_reverted(self, key: str):
        if not self.photos:
            return
        for photo in self.photos:
            photo.planned.pop(key, None)
            if not photo.dirty and photo.status == STATUS_PENDING:
                photo.status = 0
        self.refresh()
        self.planChanged.emit()

    @staticmethod
    def _parse_loose(text: str) -> datetime | None:
        text = text.strip()
        if not text:
            return None
        dt = F.parse_dt(text.replace("-", ":", 2) if text[:4].isdigit() and text[4] == "-" else text)
        if dt:
            return dt
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%Y-%m-%d", "%Y%m%d_%H%M%S", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    # ---------------------------------------------------------------- 元数据树
    def _fill_tree(self):
        self._loading = True
        self.tree.blockSignals(True)
        self.tree.clear()
        photos = self.photos
        if not photos:
            self.tree.blockSignals(False)
            self._loading = False
            return

        multi = len(photos) > 1
        merged: dict[str, list] = {}
        for photo in photos:
            for key, val in photo.meta.items():
                if key == "SourceFile":
                    continue
                merged.setdefault(key, []).append(val)

        groups: dict[str, list[tuple[str, list]]] = {}
        for key, vals in merged.items():
            group = key.split(":", 1)[0] if ":" in key else "其它"
            if group in ("File", "System") and key.split(":", 1)[-1] in (
                    "FileName", "Directory", "SourceFile"):
                continue
            groups.setdefault(group, []).append((key, vals))

        order = ["EXIF", "XMP", "IPTC", "QuickTime", "Track", "Media", "Keys",
                 "ItemList", "UserData", "PNG", "JFIF", "ICC_Profile", "File", "Composite"]
        names = sorted(groups.keys(), key=lambda g: (order.index(g) if g in order else 99, g))

        filt = self.filter_edit.text().strip().lower()
        only_writable = self.editable_only.isChecked()

        for group in names:
            items = sorted(groups[group], key=lambda kv: F.group_sort_key(kv[0]))
            visible = []
            for key, vals in items:
                if only_writable and not F.writable(key):
                    continue
                uniq = {str(v) for v in vals}
                text = vals[0] if len(uniq) == 1 else "（多个不同值）"
                text = str(text)
                tag = key.split(":", 1)[-1]
                tag_disp = F.FIELD_CN.get(tag, tag)
                # 中文名/英文 key/值 三样都能搜到(树里显示的是中文名)
                if filt and filt not in key.lower() and filt not in tag_disp.lower() \
                        and filt not in text.lower():
                    continue
                visible.append((key, text))
            if not visible:
                continue
            group_disp = F.GROUP_CN.get(group, group)
            parent = QTreeWidgetItem([f"{group_disp}  ·  {len(visible)}", ""])
            font = QFont()
            font.setWeight(QFont.DemiBold)
            parent.setFont(0, font)
            parent.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(parent)
            for key, text in visible:
                tag = key.split(":", 1)[-1]
                tag_disp = F.FIELD_CN.get(tag, tag)
                # v198: 显示层去掉东八时区; 原值另存 UserRole / tooltip,
                # 编辑器仍用原值(见 _MetaEditDelegate.setEditorData), 绝不把
                # 去掉时区的显示值写回文件。
                raw = str(text)
                child = QTreeWidgetItem([tag_disp, F.display_value(raw)])
                child.setData(0, Qt.UserRole, key)
                child.setData(1, Qt.UserRole, raw)
                child.setToolTip(0, key)
                child.setToolTip(1, raw)
                key_writable = F.writable(key)
                # 委托靠这个标记决定"这一行能不能弹输入框"(列 0 永远不给编辑)
                child.setData(1, _MetaEditDelegate.ROLE_WRITABLE, key_writable)
                if key_writable:
                    child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
                else:
                    child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    child.setForeground(1, QColor(T.TEXT_WEAK))
                    child.setToolTip(1, f"{raw}\n（只读字段，不能修改）")
                pending_value = None
                for photo in photos:
                    if key in photo.pending:
                        pending_value = photo.pending[key]
                        break
                if pending_value is not None:
                    raw_pending = str(pending_value)
                    child.setText(1, F.display_value(raw_pending))
                    child.setData(1, Qt.UserRole, raw_pending)
                    child.setForeground(1, QColor(T.ACCENT))
                    font2 = child.font(1)
                    font2.setWeight(QFont.DemiBold)
                    child.setFont(1, font2)
                parent.addChild(child)
            parent.setExpanded(bool(filt) or group in ("EXIF", "XMP", "QuickTime"))
        self.tree.blockSignals(False)
        self._loading = False

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        # 只有"值"列可以改; 字段名列与只读字段即使收到改动也一律忽略
        if self._loading or column != 1:
            return
        key = item.data(0, Qt.UserRole)
        if not key or not self.photos:
            return
        if not bool(item.data(1, _MetaEditDelegate.ROLE_WRITABLE)):
            self._loading = True
            raw = str(self.photos[0].meta.get(key, ""))
            item.setText(1, F.display_value(raw))
            item.setData(1, Qt.UserRole, raw)
            self._loading = False
            return
        value = item.text(1).strip()
        for photo in self.photos:
            original = photo.meta.get(key)
            if value == "" or (original is not None and str(original) == value):
                photo.pending.pop(key, None)
            else:
                photo.pending[key] = value
                photo.status = STATUS_PENDING
        font = item.font(1)
        font.setWeight(QFont.DemiBold if value else QFont.Normal)
        item.setFont(1, font)
        item.setForeground(1, QColor(T.ACCENT if value else T.TEXT))
        # v198: 编辑器写回的是完整原值/新值(pending 用它), 显示仍不带时区
        self._loading = True
        item.setText(1, F.display_value(value))
        item.setData(1, Qt.UserRole, value)
        self._loading = False
        self.editsChanged.emit()
        self.planChanged.emit()

    # ---------------------------------------------------------------- 工具
    def apply_time_to_all(self, photos: list[Photo]):
        src = self.photos[0] if self.photos else None
        if not src:
            return
        for photo in photos:
            for key in (F.TAKEN, F.CREATED, F.MODIFIED):
                dt = src.planned.get(key) or F.parse_dt(src.raw(key))
                if dt:
                    photo.planned[key] = dt
            photo.status = STATUS_PENDING
        self.planChanged.emit()
