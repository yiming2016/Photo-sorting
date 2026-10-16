# -*- coding: utf-8 -*-
"""右侧信息面板: 时间优先 + 全量元数据(可编辑)。"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDateTimeEdit, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea,
                               QSizePolicy, QStyle, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import theme as T
from ..core import fields as F
from ..core.photo import Photo, STATUS_PENDING
from .widgets import CheckCheckBox
from .dialogs import _DateTimeSegments


class _ClickableLabel(QLabel):
    """可点击的 QLabel（用于预览图：单击 → 用系统默认看图软件打开）。"""

    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


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

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(6)

        cap = QLabel(label_text)
        cap.setProperty("muted", True)
        cap.setMinimumWidth(60)
        lay.addWidget(cap)

        self.segments = _DateTimeSegments(datetime(2000, 1, 1, 0, 0, 0))
        self.segments.changed.connect(self._on_changed)
        lay.addWidget(self.segments, 1)

        self.edit = self.segments

        self.btn = QPushButton("\u21ba")
        self.btn.setFixedSize(28, 28)
        self.btn.setToolTip("撤销这一项")
        self.btn.setVisible(False)
        self.btn.clicked.connect(lambda: self.reverted.emit(self.key))
        lay.addWidget(self.btn)

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

        # 上组(预览 + 文件名 + 时间): 固定, 不滚动 — 始终完整显示
        top = QWidget()
        self.body_layout = QVBoxLayout(top)
        self.body_layout.setContentsMargins(12, 12, 12, 16)
        self.body_layout.setSpacing(10)
        outer.addWidget(top)

        # 预览（单击 → 用系统默认看图软件打开）
        self.preview = _ClickableLabel()
        self.preview.setFixedHeight(130)  # 压缩, 让上方的三个时间字段(尤其拍摄时间)更可能一屏显示
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setCursor(Qt.PointingHandCursor)
        self.preview.setToolTip("单击用系统默认看图软件打开")
        self.preview.setStyleSheet(
            f"background: {T.THUMB_BG}; border-radius: 10px; color: {T.TEXT_WEAK};")
        self.preview.clicked.connect(self._open_current)
        self.body_layout.addWidget(self.preview)

        # 文件名 + 文件大小(同占一行, 大小在文件名右侧) + 右侧「定位」图标
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
        name_row.addWidget(self.size_label, 0)
        name_row.addStretch(1)
        self.reveal_btn = QPushButton()
        self.reveal_btn.setObjectName("revealBtn")
        self.reveal_btn.setFixedSize(26, 26)
        self.reveal_btn.setCursor(Qt.PointingHandCursor)
        self.reveal_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.reveal_btn.setToolTip("在资源管理器中显示该文件的位置")
        self.reveal_btn.clicked.connect(self._reveal_current)
        self.reveal_btn.setVisible(False)
        name_row.addWidget(self.reveal_btn, 0, Qt.AlignTop)
        self.body_layout.addLayout(name_row)

        self.body_layout.addWidget(self._divider())

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
        self.body_layout.addLayout(head)

        self.time_rows = {}
        for key in (F.MODIFIED, F.CREATED, F.TAKEN):
            field = _TimeSegField(key, F.SYNTHETIC_LABEL[key])
            field.changed.connect(lambda _p, k=key: self._on_time_edited(k))
            field.reverted.connect(self._on_time_reverted)
            self._rows[key] = field  # 复用 _rows 字典, 兼容 set_photos / _on_time_edited
            self.time_rows[key] = field
            self.body_layout.addWidget(field)

        self.apply_all_btn = QPushButton("把当前时间应用到全部选中")
        self.apply_all_btn.setObjectName("applyAll")
        self.apply_all_btn.setVisible(False)
        self.body_layout.addWidget(self.apply_all_btn)

        self.body_layout.addWidget(self._divider())

        # 元数据树
        head2 = QHBoxLayout()
        t2 = QLabel("全部元数据")
        t2.setProperty("h2", True)
        head2.addWidget(t2)
        head2.addStretch(1)
        self.editable_only = CheckCheckBox("仅可写")
        self.editable_only.setChecked(True)
        self.editable_only.setToolTip("隐藏只读字段(Composite / System 等)")
        self.editable_only.stateChanged.connect(lambda _=0: self.refresh())
        head2.addWidget(self.editable_only)
        # "全部元数据"标题 + 仅可写 放到滚动区外(outer), 与下面 filter+tree 固定在一起
        outer.addLayout(head2)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("搜索字段名或值…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda _=0: self.refresh())
        outer.addWidget(self.filter_edit)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setIndentation(12)
        self.tree.setFrameShape(QTreeWidget.NoFrame)
        self.tree.setVerticalScrollMode(QTreeWidget.ScrollPerPixel)
        header = self.tree.header()
        header.setSectionResizeMode(0, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, header.ResizeMode.Stretch)
        self.tree.itemChanged.connect(self._on_item_changed)

        # 元数据树占满右侧剩余高度(自带垂直滚动条); 上方预览+时间组固定不滚动
        outer.addWidget(self.tree, 1)

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {T.BORDER}; background: {T.BORDER};")
        line.setFixedHeight(1)
        return line

    # ---------------------------------------------------------------- 打开 / 定位
    def _current(self):
        """当前唯一选中的照片（多选或未选时为 None）。"""
        return self.photos[0] if len(self.photos) == 1 else None

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
        self.preview.setText("")
        self.reveal_btn.setVisible(False)
        self.preview.setCursor(Qt.ArrowCursor)
        if len(self.photos) == 1:
            photo = self.photos[0]
            self.reveal_btn.setVisible(True)
            self.preview.setCursor(Qt.PointingHandCursor)
            # 只显示文件名称 + 占用空间, 不加冒号, 不显示路径/扩展名/类型
            self.name_label.setText(photo.name)
            self.size_label.setText(self._human_size(photo.size))
            if photo.thumb and not photo.thumb.isNull():
                pix = photo.thumb.scaled(QSize(320, 320), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
                self.preview.setPixmap(pix)
            else:
                self.preview.setText("缩略图加载中…")
        elif len(self.photos) > 1:
            total = sum(p.size for p in self.photos)
            self.name_label.setText(f"已选中 {len(self.photos)} 张")
            # 只显示张数 + 合计大小, 不显示路径/名称列表
            self.size_label.setText(f"合计 {self._human_size(total)}")
            self.preview.setText(f"{len(self.photos)} 张")
        else:
            self.name_label.setText("未选择")
            self.size_label.setText("在左侧点选或框选照片")
            self.preview.setText("无预览")
        self._loading = False
        self.refresh()

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
                if filt and filt not in key.lower() and filt not in text.lower():
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
                child = QTreeWidgetItem([tag_disp, text])
                child.setData(0, Qt.UserRole, key)
                child.setToolTip(0, key)
                child.setToolTip(1, text)
                if F.writable(key):
                    child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
                else:
                    child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    child.setForeground(1, T.TEXT_WEAK)
                pending_value = None
                for photo in photos:
                    if key in photo.pending:
                        pending_value = photo.pending[key]
                        break
                if pending_value is not None:
                    child.setText(1, str(pending_value))
                    child.setForeground(1, T.ACCENT)
                    font2 = child.font(1)
                    font2.setWeight(QFont.DemiBold)
                    child.setFont(1, font2)
                parent.addChild(child)
            parent.setExpanded(bool(filt) or group in ("EXIF", "XMP", "QuickTime"))
        self.tree.blockSignals(False)
        self._loading = False

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        if self._loading or column != 1:
            return
        key = item.data(0, Qt.UserRole)
        if not key or not self.photos:
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
        item.setForeground(1, T.ACCENT if value else T.TEXT)
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
