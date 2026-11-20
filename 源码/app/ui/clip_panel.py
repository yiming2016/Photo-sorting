# -*- coding: utf-8 -*-
"""剪辑态的右栏: 片段属性 + 时间线总览。

与排序模式的 Inspector、合并模式的 MergePanel 同一位置(right_stack 下标 2)。
面板自身不改数据, 只反映主窗口传来的选中片段; 所有编辑动作都在时间线上完成。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QLabel, QPushButton, QVBoxLayout, QWidget)

from . import theme as T
from .timeline import fmt_time


def _row(name: str) -> tuple[QLabel, QLabel]:
    lab = QLabel(name)
    lab.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
    val = QLabel("—")
    val.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
    val.setWordWrap(True)
    val.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab, val


class ClipPanel(QWidget):
    """片段属性: 缩略图 + 名称/类型/轨/源起止/时长 + 时间线总览。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clip = None
        self._timeline = None
        self._photos_by_path: dict[str, object] = {}
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 10)
        root.setSpacing(10)

        title = QLabel("片段")
        title.setStyleSheet(f"color: {T.TEXT}; font-size: 15px; font-weight: 600;")
        root.addWidget(title)

        self.thumb = QLabel()
        self.thumb.setFixedHeight(120)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet(
            f"background: {T.SURFACE_ALT}; border: 1px solid {T.BORDER};"
            f" border-radius: 10px; color: {T.TEXT_WEAK}; font-size: 12px;")
        root.addWidget(self.thumb)

        self._rows = {}
        for key in ("name", "kind", "track", "in", "out", "dur", "start"):
            lab, val = _row({"name": "名称", "kind": "类型", "track": "所在轨",
                             "in": "源起点", "out": "源终点", "dur": "时长",
                             "start": "时间线位置"}[key])
            self._rows[key] = val
            root.addWidget(lab)
            root.addWidget(val)

        self.hint = QLabel("在时间线上点选一个片段查看详情。\n"
                           "拖动片段可调整顺序；点标尺可移动播放头。")
        self.hint.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        root.addStretch(1)

        # 导出还没做出来, 按"做不出的先不摆"藏起来(v118 做完再显示)
        self.export_btn = QPushButton("导出")
        self.export_btn.setEnabled(False)
        self.export_btn.setToolTip("按时间线无损拼接导出（即将提供）")
        self.export_btn.setVisible(False)
        root.addWidget(self.export_btn)

        note = QLabel("导出时: 同规格视频切点对齐 → 无损拼接；"
                      "混入图片或倍速 → 重新编码并提前提示。")
        note.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 11px;")
        note.setWordWrap(True)
        root.addWidget(note)

    # ---------------------------------------------------------------- 数据
    def set_clip(self, clip, timeline=None, photos_by_path=None):
        self._clip = clip
        self._timeline = timeline
        if photos_by_path is not None:
            self._photos_by_path = photos_by_path
        if clip is None:
            for val in self._rows.values():
                val.setText("—")
            # 顺序要紧: 先清 pixmap 再 setText
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("未选中片段")
            self.hint.setVisible(True)
            return
        self.hint.setVisible(False)
        photos_by_path = photos_by_path or {}
        photo = photos_by_path.get(clip.source)
        pm = getattr(photo, "thumb", None) if photo is not None else None
        if pm is not None and not pm.isNull():
            self.thumb.setPixmap(pm.scaled(
                self.thumb.width() - 12, self.thumb.height() - 12,
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText(clip.kind_label)

        self._rows["name"].setText(clip.name)
        self._rows["kind"].setText(clip.kind_label)
        self._rows["track"].setText(clip.track_label)
        if clip.kind == "image":
            self._rows["in"].setText("—")
            self._rows["out"].setText("—")
        else:
            self._rows["in"].setText(fmt_time(clip.src_in))
            self._rows["out"].setText(fmt_time(clip.src_out))
        self._rows["dur"].setText(fmt_time(clip.duration))
        if timeline is not None:
            start = timeline.clip_start(clip)
            self._rows["start"].setText(fmt_time(start))
        else:
            self._rows["start"].setText("—")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 缩略图跟着面板宽度重算(用保存的引用, 不重新查数据)
        if self._clip is not None:
            self.set_clip(self._clip, self._timeline, self._photos_by_path)
