# -*- coding: utf-8 -*-
"""统一格式弹窗(v200).

 UI 口径完全照搬现有模块:
 - 顶部结论条 = clip_export.py 的 LosslessDialog.banner(实色底 + 白字)
 - 参数名标色 = tools_dialog.py 的 _label_color(红=fatal / 橙=warn / 灰=参考)
 - 按钮 / 边框 / 间距 = theme.py 全局 QSS
"""
from __future__ import annotations

import os

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import theme as T
from .dialogs import _ComboHighlightDelegate
from .widgets import fit_popup_width, prepare_combo_popup
from ..merge import normalize, settings


def _banner_style(color: str) -> str:
    """v204: 颜色只落在右侧提示框(normTip), 左侧「参数目标」黑字不与绿底共用背景。

    仍把整段样式 set 到外层 banner 上 —— 子控件会按 objectName 命中规则,
    这样旧断言 `dlg.banner.styleSheet()` 里依旧能查到颜色值。
    """
    return (f"#normBanner {{ background: transparent; }}"
            f"#normTip {{ background: {color}; border-radius: 8px; }}"
            f"#normTip QLabel {{ color: #FFFFFF; }}")


def _fmt_size(num: float) -> str:
    """字节 → 人类可读(KB/MB/GB)。"""
    try:
        n = float(num or 0)
    except Exception:
        return "大小未知"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n:.0f} B"


def _file_size(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except Exception:
        return 0


class _DownComboBox(QComboBox):
    """下拉项固定向下展开。

    Qt 默认在控件下方空间不足时把弹层翻到上方，用户明确要求一律向下 ——
    super().showPopup() 之后把弹层移动回控件下沿即可(QTimer 让定位在下一轮
    事件循环执行，避开 Qt 自己的 autoflip)。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        prepare_combo_popup(self)      # v215: 弹层只留一层圆角底
        # v261: 当前项/悬浮项浅蓝圆角底 —— 与主界面顶栏下拉同款。
        # 必须开 mouseTracking, 否则 MouseOver 状态不到委托, 悬浮高亮画不出来。
        self.view().setMouseTracking(True)
        self.setItemDelegate(_ComboHighlightDelegate(self))

    def showPopup(self):
        fit_popup_width(self)          # v215: 弹层放得下最长选项
        super().showPopup()
        view = self.view()
        popup = view.window() if view is not None else None
        if popup is None:
            return

        def place():
            try:
                popup.move(self.mapToGlobal(QPoint(0, self.height())))
            except RuntimeError:      # 控件已销毁
                pass

        QTimer.singleShot(0, place)


class _FoldBanner(QFrame):
    """整行可点的折叠 header(结论条 + 参数目标标题)。"""

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        try:
            self._on_click()
        finally:
            super().mousePressEvent(event)


class _CardHeader(QWidget):
    """分组卡片标题: 规格/数量/合计分钟/合计大小/状态全部横向排在同一行;
    整行可点, 点一下通知外层卡片展开/收起。"""

    def __init__(self, card, parent=None):
        super().__init__(parent)
        self.card = card
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(5)
        self.main = QLabel("")
        self.main.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
        h.addWidget(self.main)
        self.state = QLabel("")
        self.state.setStyleSheet(f"font-size: 12px; font-weight: 600;")
        h.addWidget(self.state)
        h.addStretch(1)
        self.setToolTip("点一下展开/收起组内文件明细")

    def mousePressEvent(self, event):
        self.card.toggle_expanded()
        super().mousePressEvent(event)


class _GroupCard(QFrame):
    """可展开的分组卡片: 标题一行显示规格/数量/合计时长/合计大小/状态,
    展开后逐条列出组内文件的名称、大小、时长、编码等(重要参数排前面)。"""

    def __init__(self, group: dict, parent=None):
        super().__init__(parent)
        self.group = group
        self._expanded = False
        soft, fg = ((T.WARN_SOFT, T.WARN) if int(group.get("need_count") or 0)
                    else (T.OK_SOFT, T.OK))
        self._state_color = fg
        self.setStyleSheet(
            f"QFrame {{ background: {soft}; border-radius: 8px; }}"
            f"QLabel {{ color: {T.TEXT}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(4)

        self.toggle = _CardHeader(self)
        self._fill_header()
        lay.addWidget(self.toggle)

        self.detail = QWidget()
        self.detail_lay = QVBoxLayout(self.detail)
        self.detail_lay.setContentsMargins(12, 4, 0, 0)
        self.detail_lay.setSpacing(3)
        self._fill_detail()
        self.detail.setVisible(False)
        lay.addWidget(self.detail)

    def _fill_header(self):
        items = list(self.group.get("items") or [])
        minutes = sum(float(getattr(it, "duration", 0) or 0) for it in items) / 60.0
        total_size = sum(_file_size(getattr(it, "path", "")) for it in items)
        need = int(self.group.get("need_count") or 0)
        parts = [str(self.group.get("label") or ""),
                 f"合计 {minutes:.1f} 分钟",
                 _fmt_size(total_size)]
        arrow = "▾" if self._expanded else "▸"
        self.toggle.main.setText(arrow + " " + " · ".join(parts))
        self.toggle.state.setText("· " + ("需先转码" if need else "可直接快合"))
        self.toggle.state.setStyleSheet(
            f"color: {self._state_color}; font-size: 12px; font-weight: 600;")

    def title_text(self) -> str:
        """给验收/调试用的纯文本(规格 + 状态)。"""
        return self.toggle.main.text() + " " + self.toggle.state.text()

    def _fill_detail(self):
        from PySide6.QtCore import Qt as _Qt
        items = list(self.group.get("items") or [])
        for it in items:
            path = getattr(it, "path", "")
            spec = getattr(it, "spec", None) or {}
            name = os.path.basename(path) if path else "(未知文件)"
            codec = str(spec.get("video_codec") or "?").upper()
            w, h = spec.get("width"), spec.get("height")
            res = f"{w}×{h}" if w and h else "?"
            fps = str(spec.get("r_frame_rate") or "").replace("/1", "") or "?"
            audio = str(spec.get("audio_codec") or "?").upper()
            head = (f"{name} · {_fmt_size(_file_size(path))} · "
                    f"{getattr(it, 'duration', 0) or 0:.1f} 秒 · "
                    f"{codec} · {res} · {fps}fps · {audio}")
            lb = QLabel(head)
            lb.setWordWrap(True)
            lb.setStyleSheet("color: %s; font-size: 12px;" % T.TEXT_SUB)
            self.detail_lay.addWidget(lb)
            if getattr(it, "reasons", None):
                why = QLabel("　└ 需转码：" + "；".join(it.reasons))
                why.setWordWrap(True)
                why.setStyleSheet(f"color: {T.WARN}; font-size: 11px;")
                self.detail_lay.addWidget(why)
        if not items:
            lb = QLabel("（组内没有文件）")
            lb.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            self.detail_lay.addWidget(lb)

    def toggle_expanded(self, *_):
        self._expanded = not self._expanded
        self.detail.setVisible(self._expanded)
        self._fill_header()


class NormalizeDialog(QDialog):
    """选中视频 → 自定义统一目标 → 预览分组 → 开始生成工作副本。"""

    startRequested = Signal(object, object)   # (profile dict, normalize.NormPlan)

    def __init__(self, photos, specs=None, ffmpeg_pair=None, parent=None):
        super().__init__(parent)
        self.photos = list(photos or [])
        self.specs = dict(specs or {})
        self.ffmpeg, self.ffprobe = (ffmpeg_pair or (None, None))
        self.setWindowTitle("统一格式")
        # v203: 非模态 —— 打开后还能操作后面的合并窗口/其它窗口。
        self.setModal(False)
        self.setMinimumWidth(640)
        self.resize(720, 560)
        self._build()
        self.reload_plan()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)

        # v204: header 拆成两块 —— 左侧「▾ 参数目标」黑字(无底色), 右侧独立
        # 绿色结论框(√ + 「可以统一：...」), 视觉上隔离开; 整行仍可点折叠。
        # 绿/红只由 _refresh_banner 落在 normTip 上, 不染到「参数目标」。
        self.banner = _FoldBanner(self._toggle_params)
        self.banner.setObjectName("normBanner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        self.fold_arrow = QLabel("▾")
        self.fold_arrow.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
        bl.addWidget(self.fold_arrow)
        title = QLabel("参数目标")
        title.setObjectName("paramsTitle")
        title.setStyleSheet(
            f"color: {T.TEXT}; font-size: 13px; font-weight: 600; white-space: nowrap;")
        bl.addWidget(title)
        bl.addSpacing(10)

        # 右侧独立结论框(唯一带绿/红底色的区域)。
        self.tip_box = QFrame()
        self.tip_box.setObjectName("normTip")
        tb = QHBoxLayout(self.tip_box)
        tb.setContentsMargins(12, 9, 12, 9)
        tb.setSpacing(6)
        # ✓(U+2713) 在部分字体里缺字形会画成方框, 用中文字体都带的 √
        self.banner_icon = QLabel("√")
        self.banner_icon.setStyleSheet("font-size: 15px; font-weight: 700;")
        tb.addWidget(self.banner_icon)
        self.banner_text = QLabel("正在分析…")
        self.banner_text.setWordWrap(True)
        self.banner_text.setStyleSheet("font-size: 13px; font-weight: 600;")
        tb.addWidget(self.banner_text, 1)
        bl.addWidget(self.tip_box, 1)
        root.addWidget(self.banner)

        # v204: 拉伸窗口的富余空间优先给「分组预览」(显示更多卡片), 不再把
        # 参数区拉高导致行间距被撑开 —— 参数区按内容高度固定, 分组区吃 stretch。
        body = QVBoxLayout()
        body.setSpacing(10)
        self._build_params()
        self.params_host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        body.addWidget(self.params_host, 0)
        body.addWidget(self._build_groups(), 1)
        root.addLayout(body, 1)

        foot = QHBoxLayout()
        legend = QLabel("红=必须统一 · 橙=建议统一 · 灰=仅供参考")
        legend.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        foot.addWidget(legend)
        self.saved_tip = QLabel("")
        self.saved_tip.setStyleSheet(f"color: {T.OK}; font-size: 12px;")
        foot.addWidget(self.saved_tip)
        foot.addStretch(1)
        # v201: 去掉「取消」—— 右上角 ❌ 已经是同一个功能, 留着只会挤占底栏。
        self.save_btn = QPushButton("保存为默认")
        self.save_btn.setToolTip("记住当前档位；只新增 normalize_profile 配置键，不动已有快捷键")
        self.save_btn.clicked.connect(self._save_default)
        foot.addWidget(self.save_btn)
        self.start_btn = QPushButton("开始统一")
        self.start_btn.setProperty("accent", True)
        self.start_btn.setMinimumWidth(96)
        self.start_btn.clicked.connect(self._start)
        foot.addWidget(self.start_btn)
        root.addLayout(foot)

    def _build_params(self) -> QWidget:
        """上半部主角: 参数目标(整块可随头部收起)。分档小标题 + 参数名着色沿用「对比」窗口口径。"""
        host = QWidget()
        self.params_host = host
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(6)

        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 2, 4, 0)
        lay.setSpacing(4)

        prof = normalize.NormProfile.from_dict()

        self.codec_combo = _DownComboBox()
        for key, label in normalize.CODEC_LABELS.items():
            self.codec_combo.addItem(label, key)
        self.codec_combo.setCurrentIndex(max(0, list(normalize.CODEC_LABELS).index(prof.codec)))
        self.crf_combo = _DownComboBox()
        for v in normalize.CRF_CHOICES:
            self.crf_combo.addItem(f"{v}" + ("（高质量）" if v == 18 else
                                             "（均衡）" if v == 20 else "（省空间）"), v)
        self.crf_combo.setCurrentIndex(max(0, normalize.CRF_CHOICES.index(prof.crf)))
        self.keyint_combo = _DownComboBox()
        for v in normalize.KEYINT_CHOICES:
            self.keyint_combo.addItem(f"{v} 秒" + ("（推荐）" if v == 2 else ""), v)
        self.keyint_combo.setCurrentIndex(max(0, normalize.KEYINT_CHOICES.index(prof.keyint_sec)))
        self.audio_combo = _DownComboBox()
        for v in (128, 192, 256):
            self.audio_combo.addItem(f"AAC-LC {v}k · 48kHz（保留声道）", v)
        self.audio_combo.setCurrentIndex(max(0, (128, 192, 256).index(prof.audio_bitrate)
                                            if prof.audio_bitrate in (128, 192, 256) else 1))

        # v202: 每个档位内部两列并排(一行显示两个), 档位小标题仍跨整行。
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        def section(text, color, pairs):
            gr = grid.rowCount()
            title = QLabel(text)
            title.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: 600;"
                f" padding-left: 8px; background: {T.SURFACE_ALT}; border-radius: 4px;")
            grid.addWidget(title, gr, 0, 1, 2)
            for i, (name, c, widget) in enumerate(pairs):
                grid.addWidget(self._row(name, c, widget), gr + 1 + i // 2, i % 2)

        section("必须一致", T.DANGER, [
            ("视频编码", T.DANGER, self.codec_combo),
            ("音频", T.DANGER, self.audio_combo),
        ])
        section("建议一致", T.WARN, [
            ("关键帧间隔", T.WARN, self.keyint_combo),
        ])
        src_pairs = [("码率质量", T.TEXT_SUB, self.crf_combo)]
        for name in ("分辨率", "帧率", "色彩 / HDR", "声道布局"):
            lab = QLabel("按源分组（不统一）")
            lab.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            src_pairs.append((name, T.TEXT_SUB, lab))
        section("仅参考 / 按源分组", T.TEXT_SUB, src_pairs)

        lay.addLayout(grid)

        for combo in (self.codec_combo, self.crf_combo, self.keyint_combo, self.audio_combo):
            combo.currentIndexChanged.connect(self.reload_plan)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(box)
        outer.addWidget(scroll, 1)
        return host

    def _add_section(self, lay: QVBoxLayout, text: str, color: str):
        """跨行小标题: 与「对比」窗口的 _add_section_row 同款(软底 + 档位色)。"""
        lab = QLabel(text)
        lab.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600;"
            f" padding-left: 8px; background: {T.SURFACE_ALT}; border-radius: 4px;")
        lay.addWidget(lab)

    def _row(self, name: str, color: str, widget: QWidget) -> QWidget:
        """参数名固定宽居中, 控件拉伸填满本列 —— 两列布局每列内部左边缘对齐。

        v203: 参数名和值控件都给 24px 最小高度 —— QLabel 默认高度贴着字体基线,
        中文字形(帧/率/彩/HDR 的下缘)会被 grid 行高压掉半截, 看着"没显示完全"。
        """
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        k = QLabel(name)
        k.setFixedWidth(76)
        k.setMinimumHeight(24)
        k.setAlignment(Qt.AlignCenter)
        k.setStyleSheet(f"color: {color}; font-size: 12px;")
        h.addWidget(k)
        widget.setMinimumHeight(max(24, widget.minimumHeight()))
        h.addWidget(widget, 1)
        return row

    def _toggle_params(self):
        """点 header(箭头/标题/结论整行)收起或展开参数目标区。"""
        host = getattr(self, "params_host", None)
        if host is None:
            return
        show = not host.isVisible()
        host.setVisible(show)
        self.fold_arrow.setText("▾" if show else "▸")

    def _build_groups(self) -> QWidget:
        """下半部: 分组预览(v204: 吃满窗口拉伸的富余高度, 显示更多卡片)。

        不再设 MaximumHeight —— 之前的 210px 上限会把拉伸出来的空间浪费掉。
        """
        box = QWidget()
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        cap = QLabel("分组预览")
        cap.setStyleSheet(f"color: {T.TEXT}; font-size: 13px; font-weight: 600;")
        lay.addWidget(cap)
        self.group_area = QScrollArea()
        self.group_area.setWidgetResizable(True)
        self.group_area.setFrameShape(QFrame.NoFrame)
        self.group_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.group_area.setMinimumHeight(120)
        self.group_area.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.group_host = QWidget()
        self.group_lay = QVBoxLayout(self.group_host)
        self.group_lay.setContentsMargins(0, 4, 4, 0)
        self.group_lay.setSpacing(8)
        self.group_area.setWidget(self.group_host)
        lay.addWidget(self.group_area, 1)
        tip = QLabel("原文件不动；副本带参数指纹，源变化自动重建。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        lay.addWidget(tip)
        return box

    # ---------------------------------------------------------------- 数据
    def profile(self) -> dict:
        return {
            "codec": self.codec_combo.currentData() or "main",
            "crf": int(self.crf_combo.currentData() or 20),
            "keyint_sec": int(self.keyint_combo.currentData() or 2),
            "audio_bitrate": int(self.audio_combo.currentData() or 192),
        }

    def reload_plan(self, *_):
        try:
            self.plan = normalize.plan(
                [p.path for p in self.photos], self.ffprobe,
                normalize.NormProfile.from_dict(self.profile()),
                self.specs)
        except Exception as e:
            self.plan = normalize.NormPlan()
            self.plan.errors = [f"分析失败：{e}"]
        self._refresh_banner()
        self._refresh_groups()

    def _refresh_banner(self):
        plan = getattr(self, "plan", None)
        if plan is None:
            return
        if not self.ffprobe:
            color, icon = T.DANGER, "×"
            text = "未找到 ffprobe，无法分析。请先在合并右栏左下角指定 FFmpeg 路径。"
        elif plan.errors:
            color, icon = T.DANGER, "×"
            text = (f"{len(plan.errors)} 个文件读取失败，不能开始统一："
                    + "；".join(plan.errors[:2]))
        elif not plan.items:
            color, icon = T.DANGER, "×"
            text = "请先选中要统一的视频。"
        else:
            color, icon = T.OK, "√"
            if plan.need_count:
                text = (f"可以统一：{len(plan.items)} 个视频 → {len(plan.groups)} 个合并组；"
                        f"其中 {plan.need_count} 个需要转码，"
                        f"素材共 {plan.need_duration / 60:.1f} 分钟")
            else:
                text = (f"全部已达标：{len(plan.items)} 个视频 → {len(plan.groups)} 个合并组，"
                        "无需转码，可直接快合")
        self.banner.setStyleSheet(_banner_style(color))
        self.banner_icon.setText(icon)
        self.banner_text.setText(text)
        self.start_btn.setEnabled(not plan.errors and bool(plan.items) and bool(self.ffprobe))

    def _refresh_groups(self):
        while self.group_lay.count():
            it = self.group_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        plan = getattr(self, "plan", None)
        if plan is None:
            return
        for g in plan.groups:
            self.group_lay.addWidget(_GroupCard(g))
        if not plan.groups:
            lab = QLabel("没有可统一的文件")
            lab.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            self.group_lay.addWidget(lab)
        self.group_lay.addStretch(1)

    def _save_default(self):
        ok = settings.save_normalize_profile(self.profile())
        self.saved_tip.setText("已保存为默认" if ok else "保存失败")
        if ok:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2500, lambda: self.saved_tip.setText(""))

    def _start(self):
        plan = getattr(self, "plan", None)
        if plan is None or plan.errors or not plan.items:
            return
        self.startRequested.emit(self.profile(), plan)
