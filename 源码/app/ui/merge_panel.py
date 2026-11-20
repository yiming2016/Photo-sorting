# -*- coding: utf-8 -*-
"""合并界面的右栏: 预览 + 输出设置 + 合并方式 + 开始合并(含进度)。

与排序界面的 Inspector 是同一位置的两种形态, 由主窗口的 QStackedWidget 切换。
面板自身不做探测/合并, 只发信号:
  startRequested(bool: 是否转码) / stopRequested / ffmpegRequested / revealRequested
"""
from __future__ import annotations

import html
import os

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QSpacerItem, QVBoxLayout,
                               QWidget)

from . import theme as T
from . import compare_model
from .edit_icons import make_icon
from .widgets import set_accent
from ..merge import grouping


class _PreviewLabel(QLabel):
    """选中视频的预览图: 按可用空间等比缩放, 高度不足以显示时给文字提示。

    点一下 = 用系统默认程序打开这个文件(和「排序」右侧的预览图一致)。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setMaximumHeight(200)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点一下：用默认程序打开这个文件")
        self.setStyleSheet(
            f"background: {T.SURFACE_ALT}; border: 1px solid {T.BORDER};"
            f" border-radius: 10px; color: {T.TEXT_WEAK}; font-size: 12px;")
        self._photo = None

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._photo is not None:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def set_photo(self, photo):
        self._photo = photo
        self._repaint()

    def _repaint(self):
        photo = self._photo
        if photo is None:
            self.setText("在左侧勾选视频")
            return
        pm = getattr(photo, "thumb", None)
        if pm is not None and not pm.isNull():
            w = max(60, self.width() - 10)
            h = max(60, self.height() - 10)
            self.setPixmap(pm.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.setText("（暂无预览）")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._repaint()


def _sep() -> QWidget:
    line = QWidget()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {T.BORDER};")
    return line


def _caption(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
    return lab


def _fmt_eta(sec: float) -> str:
    """剩余秒数 -> 12 秒 / 3 分 20 秒 / 1 小时 05 分"""
    try:
        total = int(round(float(sec)))
    except Exception:
        return "—"
    if total < 0:
        return "—"
    if total < 60:
        return f"{total} 秒"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m} 分 {s:02d} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m:02d} 分"


class MergePanel(QWidget):
    startRequested = Signal(bool)     # True = 转码合并
    stopRequested = Signal()
    ffmpegRequested = Signal()
    exiftoolRequested = Signal()
    revealRequested = Signal()
    outputDirRequested = Signal()     # 用户点了「选择…」换输出目录
    paramLevelsRequested = Signal()   # 用户点了 ⚙ 打开参数分档设置
    previewRequested = Signal()       # 点了预览图: 用默认程序打开
    editRequested = Signal()          # 点了「剪辑」: 打开剪辑窗口
    normalizeRequested = Signal()     # 点了「统一格式」: 打开统一格式弹窗(v200)
    cleanNormRequested = Signal()     # v217: ⚙ → 清理统一格式副本

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._photos: list = []
        self._specs: dict = {}
        self._errors: dict = {}
        self._ffmpeg_ok = False
        self._ffmpeg_state = None     # ⚙ 的颜色用(原来的 FFmpeg 按钮状态)
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 10)
        root.setSpacing(10)

        # 输出文件名不再单独占一行: 由程序自动起名(合并_001.mp4 往后排),
        # 控件保留(隐藏)让主窗口的既有逻辑照常读写
        self.name_edit = QLineEdit()
        self.name_edit.hide()

        # 顶部第一行: 「自动」开关 + 无损/转码 + ⚙ 分档设置(压窄, 一行放四个)
        seg = QHBoxLayout()
        seg.setSpacing(6)
        self.auto_btn = QPushButton("自动")
        self.auto_btn.setCheckable(True)
        self.auto_btn.setChecked(True)
        self.auto_btn.setProperty("seg", "true")
        self.auto_btn.setObjectName("mergeAutoBtn")
        self.auto_btn.setToolTip("开启后自动选无损（规格不一致时自动转码）；关闭后手动选")
        self.auto_btn.setCursor(Qt.PointingHandCursor)
        seg.addWidget(self.auto_btn)
        self.lossless_btn = QPushButton("无损合并")
        self.reencode_btn = QPushButton("转码合并")
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for btn in (self.lossless_btn, self.reencode_btn):
            btn.setCheckable(True)
            btn.setProperty("seg", "true")
            btn.setProperty("narrow", "true")
            btn.setCursor(Qt.PointingHandCursor)
            self._group.addButton(btn)
            seg.addWidget(btn)
        self.lossless_btn.setChecked(True)
        self.lossless_btn.setToolTip("同规格视频直接拼接，速度快、画质无损")
        self.reencode_btn.setToolTip("规格不一致时重编码拼接，慢，画质会有损失")
        # ⚙ = 设置菜单: 参数分档 + FFmpeg 路径/检测(原来左下角那个 FFmpeg 按钮挪进来了)
        # ⚠️ 齿轮用自绘图标, 不用 "⚙" 字符 —— 微软雅黑 / Segoe UI Symbol 都没这个字形,
        #    按钮上会是一个空心方块。图标颜色还能顺手表达 FFmpeg 的状态。
        self.gear_btn = QPushButton()
        self.gear_btn.setObjectName("mergeGearBtn")
        self.gear_btn.setIcon(make_icon("gear", T.TEXT_SUB, 18))
        self.gear_btn.setIconSize(QSize(18, 18))
        self.gear_btn.setToolTip("设置：参数分档 / FFmpeg")
        self.gear_btn.setCursor(Qt.PointingHandCursor)
        self.gear_btn.clicked.connect(self._show_gear_menu)
        seg.addWidget(self.gear_btn)
        root.addLayout(seg)

        # 保存位置行: 标签 + 输入框 + 选择按钮(原「合并名称」「输出位置」两行合并成一行)
        loc = QHBoxLayout()
        loc.setSpacing(8)
        title = QLabel("保存位置")
        title.setStyleSheet(f"color: {T.TEXT}; font-size: 14px; font-weight: 600;")
        loc.addWidget(title)
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("（默认与第一个视频同目录，可直接修改）")
        loc.addWidget(self.dir_edit, 1)
        self.dir_btn = QPushButton("选择…")
        self.dir_btn.setFixedWidth(64)
        self.dir_btn.clicked.connect(self._pick_dir)
        loc.addWidget(self.dir_btn)
        root.addLayout(loc)

        self.preview = _PreviewLabel()
        self.preview.clicked.connect(self.previewRequested)
        root.addWidget(self.preview)

        self.summary = QLabel("还没有勾选视频")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        root.addWidget(self.summary)

        # v251: 删掉「参数对比」标题文字与其上方的横线(_sep), 对比清单直接紧接摘要显示。
        # hint 结论那句话仍保留在 head 里, 单独一行显示。
        head = QHBoxLayout()
        head.setSpacing(8)
        head.setContentsMargins(0, 0, 0, 0)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        head.addWidget(self.hint, 1)
        root.addLayout(head)

        self.compare_scroll = QScrollArea()
        self.compare_scroll.setWidgetResizable(True)
        self.compare_scroll.setFrameShape(QFrame.NoFrame)
        self.compare_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 不再限高: 对比区吃掉右侧所有富余空间(以前卡在 180px, 十几项只能露 9 行)。
        self.compare_scroll.setMinimumHeight(120)
        self.compare_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.compare_body = QLabel()
        self.compare_body.setWordWrap(True)
        self.compare_body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.compare_body.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
        # v255: ⓘ 角标立即弹说明窗(绕开富文本 <a title> tooltip 不弹/延迟)
        self._info_filter = compare_model.InfoPopupFilter(self.compare_body)
        self.compare_scroll.setWidget(self.compare_body)
        root.addWidget(self.compare_scroll, 1)

        # 进度: 条内显示百分比, 条下显示"剩余时间"
        self.progress = QProgressBar()
        self.progress.setObjectName("mergeProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        self.progress_label.setVisible(False)
        root.addWidget(self.progress_label)

        # v202: 四个按钮强制同一行 —— 用户明确要求; 右栏上限 360px, 所以统一收窄
        # 最小宽度 + 取消 stretch, 让按钮按内容自适应, 挤在同一行也不重叠。
        bottom = QHBoxLayout()
        bottom.setSpacing(6)

        # v202: 「开始合并」→「顺序合并」(按文件顺序逐个合并的语义更准确)
        self.start_btn = QPushButton("顺序合并")
        self.start_btn.setToolTip("按文件顺序逐个无损合并；参数不一致时可选转码合并")
        self.start_btn.setCursor(Qt.PointingHandCursor)
        set_accent(self.start_btn, True)
        self.start_btn.clicked.connect(self._on_start)
        bottom.addWidget(self.start_btn)

        # v234: 删除「打开位置」按钮 —— 右键菜单「打开文件所在位置」已覆盖该能力。
        # v209: 删除「统一格式」按钮 —— 统一已升级为顶栏蓝按钮切换的大形态,
        # 合并右栏底部只留 顺序合并 | 剪辑 两按钮。

        # 「剪辑」: 一行最右端, 绿色 —— 它是"接着去剪"的正向动作
        self.edit_btn = QPushButton("剪辑")
        self.edit_btn.setObjectName("mergeEditBtn")
        self.edit_btn.setCursor(Qt.PointingHandCursor)
        self.edit_btn.setToolTip("打开剪辑窗口（素材先进素材框，点 ＋ 加入时间线）")
        self.edit_btn.setStyleSheet(
            f"QPushButton#mergeEditBtn{{background:{T.OK}; border:1px solid {T.OK};"
            f" color:#FFFFFF; font-weight:600; padding:5px 16px; border-radius:6px;}}"
            f"QPushButton#mergeEditBtn:hover{{background:#1A7E48; border-color:#1A7E48;}}"
            f"QPushButton#mergeEditBtn:pressed{{background:#15663A; border-color:#15663A;}}")
        self.edit_btn.clicked.connect(self.editRequested)
        bottom.addWidget(self.edit_btn)

        root.addLayout(bottom)

        self.lossless_btn.toggled.connect(lambda *_: self._refresh_segment())
        self.auto_btn.toggled.connect(lambda *_: self._refresh_segment())
        self._show_compare_hint()
        self._refresh_segment()

    # ---------------------------------------------------------------- 数据
    def _show_gear_menu(self):
        """⚙ 菜单: 参数分档 + FFmpeg 路径/检测。

        原来左下角那个「FFmpeg」按钮挪进来了 —— 底部那行只留合并相关的动作。
        那个按钮当时用绿/红表示检测结果, 这条信息改由 ⚙ 自己的颜色继续表达。
        """
        m = QMenu(self)
        m.setCursor(Qt.PointingHandCursor)      # 菜单项悬浮也是抓手
        a1 = m.addAction("参数分档设置…")
        a1.triggered.connect(lambda *_: self.paramLevelsRequested.emit())
        # ExifTool(元数据引擎): 挂在 FFmpeg 上面, 用户要求的位置
        et_label = "ExifTool 路径 / 更新…"
        try:
            from ..core.exiftool import find_exiftool
            et_label += "（已就绪）" if find_exiftool() else "（未找到 ExifTool）"
        except Exception:
            pass
        a_et = m.addAction(et_label)
        a_et.triggered.connect(lambda *_: self.exiftoolRequested.emit())
        label = "FFmpeg 路径 / 检测…"
        if self._ffmpeg_state is True:
            label += "（已就绪）"
        elif self._ffmpeg_state is False:
            label += "（没有可用的 FFmpeg）"
        a2 = m.addAction(label)
        a2.triggered.connect(lambda *_: self.ffmpegRequested.emit())
        m.addSeparator()
        # v217: 统一副本以前没有任何清理入口 —— 换档位/换片子后旧的几十 GB 一直躺着
        a3 = m.addAction("清理统一格式副本…")
        a3.setToolTip("删掉当前文件夹下自动生成的统一副本（原视频不受影响）")
        a3.triggered.connect(lambda *_: self.cleanNormRequested.emit())
        m.exec(self.gear_btn.mapToGlobal(QPoint(0, self.gear_btn.height() + 2)))

    def set_ffmpeg_state(self, ok):
        """FFmpeg 状态: True=可用 / False=不可用 / None=检测中。

        原来靠左下角「FFmpeg」按钮的绿/红表达, 按钮挪进 ⚙ 之后改在 ⚙ 自己身上体现。
        """
        self._ffmpeg_state = ok
        base = ('QPushButton#mergeGearBtn{border-radius:6px; padding:6px 2px;'
                ' min-width:34px; max-width:34px;')
        if ok is True:
            self.gear_btn.setIcon(make_icon("gear", T.OK, 18))
            self.gear_btn.setStyleSheet(
                base + f" background:{T.OK_SOFT}; border:1px solid {T.OK};}}")
            self.gear_btn.setToolTip("设置（FFmpeg 已就绪）：参数分档 / FFmpeg")
        elif ok is False:
            self.gear_btn.setIcon(make_icon("gear", T.DANGER, 18))
            self.gear_btn.setStyleSheet(
                base + f" background:{T.DANGER_SOFT};"
                       " border:1px solid #F0BEBE;}")
            self.gear_btn.setToolTip("设置（没有可用的 FFmpeg，点开指定路径）")
        else:
            self.gear_btn.setIcon(make_icon("gear", T.TEXT_SUB, 18))
            self.gear_btn.setStyleSheet("")
            self.gear_btn.setToolTip("设置（正在检查 FFmpeg…）：参数分档 / FFmpeg")

    def set_context(self, specs: dict, errors: dict, ffmpeg_ok: bool):
        self._specs = specs or {}
        self._errors = errors or {}
        self._ffmpeg_ok = bool(ffmpeg_ok)
        self._refresh_hint()

    def set_selection(self, photos: list):
        self._photos = list(photos)
        self.preview.set_photo(self._photos[0] if self._photos else None)
        self._refresh_summary()
        self._refresh_buttons()
        self._refresh_hint()
        self._refresh_segment()   # 规格不一致时自动切到「转码合并」
        self._refresh_compare()

    def _refresh_compare(self):
        """选中视频后列出参数对比(单选显示该视频自己的参数, 多选按三档对比);
        冲突时才按致命度标色, 一致显示绿色(与右键「对比」同一套规则)。

        与右键「对比」窗口同一套规则(compare_model): 三档小标题 + 一致绿 / 冲突红橙。
        对比区常驻: 什么都没选时显示灰字提示。
        """
        specs = [self._specs.get(p.path) for p in self._photos
                 if self._specs.get(p.path)]
        if not self._photos or not specs:
            self._show_compare_hint()
            return
        rows = compare_model.build_rows(specs, compact=True)
        if not rows:
            self._show_compare_hint()
            return

        parts, prev = [], object()
        for i, row in enumerate(rows):
            if row["level"] != prev:
                if parts:
                    parts.append("")
                    # v251: 「建议一致」「仅供参考」档位上方加蓝色加粗横线; 首档(必须一致)不加
                    if row["level"] in ("warn", None):
                        parts.append(compare_model.SECTION_LINE_HTML)
                sc = (T.DANGER if row["level"] == "fatal"
                      else T.WARN if row["level"] == "warn" else T.TEXT_SUB)
                parts.append(
                    f'<span style="color:{sc}; font-size:13px; font-weight:600">'
                    f'{html.escape(compare_model.section_title(row["level"]))}</span>')
                prev = row["level"]
            elif compare_model.media_divider_before(rows, i):
                parts.append(compare_model.DIVIDER_HTML)
            # 同一个值只写一次(三段都是 hevc 就只写 "hevc"), 右侧窄栏读起来更清楚
            uniq = []
            for v in row["values"]:
                if v not in uniq:
                    uniq.append(v)
            val_text = " / ".join(html.escape(v) for v in uniq)
            parts.append(f'<span style="color:{row["color"]}">{html.escape(row["cn"])}</span>'
                         f'{compare_model.info_html(row["key"])}'
                         f'：<span style="color:{T.TEXT}">{val_text}</span>')

        self.compare_body.setText(compare_model.join_rows(parts))

    def _show_compare_hint(self):
        n = len(self._photos)
        if n == 0:
            txt = "在左侧勾选视频后，这里显示所选视频的参数对比。"
        elif not self._specs:
            txt = "正在检测视频规格…"
        else:
            txt = "所选视频还没检测出规格，稍候再试。"
        self.compare_body.setText(
            f'<span style="color:{T.TEXT_WEAK};">{txt}</span>')

    def out_name(self) -> str:
        return self.name_edit.text().strip()

    def out_dir(self) -> str:
        return self.dir_edit.text().strip()

    def default_dir(self) -> str:
        if self._photos:
            return os.path.dirname(self._photos[0].path)
        return ""

    def set_default_name(self, name: str):
        if not self.name_edit.text().strip():
            self.name_edit.setText(name)

    def set_dir(self, path: str):
        self.dir_edit.setText(path)

    def is_reencode(self) -> bool:
        return self.reencode_btn.isChecked()

    # ---------------------------------------------------------------- 状态
    def _refresh_summary(self):
        self.summary.setText(compare_model.selection_summary(self._photos, self._specs))

    def _refresh_buttons(self):
        n = len(self._photos)
        # 合并进行中: 「开始合并」已变成「取消」, 必须保持可点 —— 否则取消按钮是死的
        self.start_btn.setEnabled(True if self._busy else
                                  (self._ffmpeg_ok and n >= 2))
        auto = self.auto_btn.isChecked()
        self.auto_btn.setEnabled(not self._busy)
        self.gear_btn.setEnabled(not self._busy)
        self.lossless_btn.setEnabled(not self._busy and not auto and self._same_spec())
        self.reencode_btn.setEnabled(not self._busy and not auto and n >= 2)
        self.dir_btn.setEnabled(not self._busy)
        self.name_edit.setEnabled(not self._busy)

    def _same_spec(self) -> bool:
        if len(self._photos) < 2:
            return True
        sigs = {grouping.signature(self._specs.get(p.path)) for p in self._photos}
        return len(sigs) == 1

    def _refresh_hint(self):
        n = len(self._photos)
        if not self._ffmpeg_ok:
            self.hint.setText("未找到 FFmpeg，点 ⚙ 指定")
            self.hint.setStyleSheet(f"color: {T.DANGER}; font-size: 12px;")
            self._refresh_buttons()
            return
        if n == 0:
            self.hint.setText("勾选 2 段以上视频")
            self.hint.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        elif n == 1:
            self.hint.setText("再选 1 段")
            self.hint.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        elif self._same_spec():
            self.hint.setText("可无损合并")
            self.hint.setStyleSheet(f"color: {T.OK}; font-size: 12px;")
        else:
            self.hint.setText("【转码合并】(较慢)")
            self.hint.setStyleSheet(f"color: {T.WARN}; font-size: 12px;")
        self._refresh_buttons()

    def _refresh_segment(self):
        # 「自动无损」开着: 替用户选好 —— 同规格选无损, 不一致自动切转码
        if self.auto_btn.isChecked():
            want = self.lossless_btn if self._same_spec() else self.reencode_btn
            if not want.isChecked():
                want.setChecked(True)
        elif not self._same_spec() and self.lossless_btn.isChecked():
            # 手动模式下规格不一致时不允许选「无损」(选了也会在 worker 里失败)
            self.reencode_btn.setChecked(True)
        self._refresh_buttons()

    def set_busy(self, busy: bool):
        self._busy = busy
        self.progress.setVisible(busy)
        self.progress_label.setVisible(busy)
        if busy:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress_label.setText("正在启动 ffmpeg…")
            self.start_btn.setText("取消")
            set_accent(self.start_btn, False)
            self.start_btn.setProperty("danger", "true")
            self._repolish(self.start_btn)
        else:
            self.progress_label.setText("")
            self.start_btn.setText("顺序合并")
            self.start_btn.setProperty("danger", "false")
            set_accent(self.start_btn, True)
            self._repolish(self.start_btn)
        self._refresh_buttons()

    @staticmethod
    def _repolish(w):
        w.style().unpolish(w)
        w.style().polish(w)

    def set_progress(self, pct: int, eta: float = -1.0):
        """pct: 0~100; eta: 预计剩余秒数(<0 表示还估不出来)。"""
        self.progress.setValue(max(0, min(100, int(pct))))
        if eta is not None and eta >= 0:
            self.progress_label.setText(
                f"已合并 {int(pct)}% · 剩余约 {_fmt_eta(eta)}")
        else:
            self.progress_label.setText(f"已合并 {int(pct)}% · 正在估算剩余时间…")

    def mark_cancelling(self):
        """点了「取消」: 等 ffmpeg 退出并清理半成品, 期间按钮置灰防连点。"""
        self.start_btn.setEnabled(False)
        self.start_btn.setText("正在取消…")
        self.progress_label.setText("正在停止 ffmpeg 并清理临时文件…")

    # v234: 删除「打开位置」按钮及其 set_reveal_enabled（右键菜单已覆盖）

    # ---------------------------------------------------------------- 交互
    def _pick_dir(self):
        start = self.dir_edit.text().strip() or self.default_dir() or os.getcwd()
        path = QFileDialog.getExistingDirectory(self, "选择输出文件夹", start)
        if path:
            self.dir_edit.setText(path)

    def _on_start(self):
        if self._busy:
            self.stopRequested.emit()
            return
        self.startRequested.emit(self.reencode_btn.isChecked())
