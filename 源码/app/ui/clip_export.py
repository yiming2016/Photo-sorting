# -*- coding: utf-8 -*-
"""剪辑导出的判定弹窗: 能不能无损、为什么不能、要不要硬着头皮转码。

一条铁律贯穿整个弹窗: **不能无损时必须把话说白** —— 顶部红条直接写
「不建议剪辑」, 导出按钮写成「仍要转码导出（慢 · 画质有损）」,
绝不让用户在不知情的情况下等一次重新编码。

两种用法:
- `readonly=True`: 点工具条状态胶囊打开, 只看原因 + 一键修复, 不导出;
- `readonly=False`: 点「导出」打开, 带输出路径, 底部是真正的导出按钮。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from . import theme as T
from ..core.lossless import FATAL, INFO, WARN

_ICON = {FATAL: "×", WARN: "!", INFO: "i"}
_COLOR = {FATAL: T.DANGER, WARN: T.WARN, INFO: T.TEXT_WEAK}


def _fmt_dur(t: float) -> str:
    t = max(0.0, float(t))
    m = int(t // 60)
    return f"{m}:{t - m * 60:04.1f}"


# 估不出源文件大小时按这个码率折(1080p 常见值), 只当参考
_DEF_V_BPS = 8_000_000
_IMG_BPS = 4_000_000


def estimate_size(plan) -> float:
    """估一下导出文件多大(字节)。

    无损导出是"原样搬字节", 所以按每段占源时长的比例乘源文件大小, 结果基本准;
    转码 / 图片 / 拿不到源文件时才退回按码率估(这时只能算个大概)。
    """
    total = 0.0
    for s in list(getattr(plan, "segments", None) or []):
        dur = max(0.0, float(getattr(s, "duration", 0.0) or 0.0))
        if dur <= 0:
            continue
        src = str(getattr(s.clip, "source", "") or "")
        try:
            fsize = float(os.path.getsize(src)) if src else 0.0
        except OSError:
            fsize = 0.0
        sdur = float(getattr(s.clip, "src_duration", 0.0) or 0.0)
        if getattr(s, "kind", "") != "image" and fsize > 0 and sdur > 0.05:
            span = min(sdur, dur * max(0.05, float(getattr(s, "speed", 1.0) or 1.0)))
            total += fsize * (span / sdur)
            continue
        bps = _IMG_BPS if getattr(s, "kind", "") == "image" else _DEF_V_BPS
        total += bps * dur / 8.0
    return total


def _fmt_size(n: float) -> str:
    n = max(0.0, float(n))
    if n >= 1024 ** 3:
        return f"约 {n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"约 {n / 1024 ** 2:.0f} MB"
    return f"约 {n / 1024:.0f} KB"


def _esc(s) -> str:
    """富文本要转义, 文件名里带 <> & 很常见。"""
    return (str(s or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _reason_html(r) -> str:
    """把一条原因拼成富文本: 故障名加粗 + 素材名 + 参数(标签加粗) + 后果。"""
    effect_color = T.DANGER if r.level == FATAL else "#B45309"
    rows = [f'<div style="font-weight:700; font-size:13px;">{_esc(r.title)}</div>']
    if r.subject:
        rows.append(f'<div style="color:{T.TEXT_SUB}; margin-top:2px;">'
                    f'{_esc(r.subject)}</div>')
    for label, value in (r.items or []):
        rows.append(f'<div style="margin-top:3px;"><b>{_esc(label)}</b>'
                    f'{_esc(value)}</div>')
    if r.effect:
        rows.append(f'<div style="color:{effect_color}; font-weight:600;'
                    f' margin-top:3px;">可能的结果：{_esc(r.effect)}</div>')
    return "".join(rows)


class LosslessDialog(QDialog):
    """导出前的判定弹窗(也可只读查看)。"""

    fixed = Signal(str)          # 用户点了某条的「修复」

    def __init__(self, parent=None, plan=None, evaluate=None, fix=None,
                 readonly: bool = False, default_out: str = ""):
        super().__init__(parent)
        self.plan = plan
        self._evaluate = evaluate      # 回调: 重新判定(修复后要刷新)
        self._fix = fix                # 回调: 执行一键修复
        self._readonly = bool(readonly)
        self._sized = False
        self.out_path = default_out or ""
        self.setWindowTitle("为什么不能无损" if readonly else "导出剪辑")
        self.setMinimumWidth(600)
        self.resize(620, 420)
        self.setModal(True)
        self._build()
        self.reload()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        # ---- 结论条 ----
        self.banner = QFrame()
        self.banner.setObjectName("lossBanner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 10, 14, 10)
        self.banner_icon = QLabel("✓")
        self.banner_icon.setStyleSheet("font-size: 18px; font-weight: 700;")
        bl.addWidget(self.banner_icon)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        self.banner_text.setStyleSheet("font-size: 14px; font-weight: 600;")
        bl.addWidget(self.banner_text, 1)
        root.addWidget(self.banner)

        # ---- 时长 / 输出 ----
        self.meta = QLabel("")
        self.meta.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        root.addWidget(self.meta)

        self.out_row = QWidget()
        ol = QHBoxLayout(self.out_row)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(6)
        lab = QLabel("输出到")
        lab.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        ol.addWidget(lab)
        self.out_edit = QLineEdit()
        self.out_edit.setReadOnly(True)
        ol.addWidget(self.out_edit, 1)
        self.pick_btn = QPushButton("浏览…")
        self.pick_btn.setFocusPolicy(Qt.NoFocus)
        self.pick_btn.setCursor(Qt.PointingHandCursor)
        self.pick_btn.clicked.connect(self._pick)
        ol.addWidget(self.pick_btn)
        root.addWidget(self.out_row)
        self.out_row.setVisible(not self._readonly)

        # ---- 原因清单 ----
        tip = QLabel("这次导出的情况：")
        tip.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        root.addWidget(tip)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.body_lay.setSpacing(8)
        self.body_lay.addStretch(1)
        self.scroll.setWidget(self.body)
        self.scroll.setMinimumHeight(150)
        root.addWidget(self.scroll, 1)

        # ---- 底部按钮 ----
        # 只读模式(看原因)不摆任何按钮: 右上角的 × 已经能关, 再放个「关闭」是多余的。
        # v152: 导出模式也不再摆「取消」—— 右上角一样能关, 底部只留一个「导出」。
        if not self._readonly:
            row = QHBoxLayout()
            row.addStretch(1)
            self.cancel_btn = None
            self.go_btn = QPushButton("导出")
            self.go_btn.setProperty("accent", True)
            self.go_btn.setFocusPolicy(Qt.NoFocus)
            self.go_btn.setCursor(Qt.PointingHandCursor)
            self.go_btn.clicked.connect(self._go)
            row.addWidget(self.go_btn)
            root.addLayout(row)
        else:
            self.cancel_btn = None
            self.go_btn = None

    # ---------------------------------------------------------------- 内容
    def reload(self):
        """按当前 plan 重画(修复后调用)。"""
        if self._evaluate is not None:
            try:
                self.plan = self._evaluate()
            except Exception:
                pass
        plan = self.plan
        if plan is None:
            return

        ok = bool(plan.ok) and plan.exportable
        warn_only = ok and bool(plan.warn_reasons)
        color = T.OK if ok and not warn_only else (T.WARN if warn_only else T.DANGER)
        self.banner.setStyleSheet(
            f"#lossBanner {{ background: {color}; border-radius: 8px; }}"
            f"#lossBanner QLabel {{ color: #FFFFFF; }}")
        if not plan.exportable:
            self.banner_icon.setText("×")
            self.banner_text.setText(plan.summary())
        elif ok:
            self.banner_icon.setText("✓")
            self.banner_text.setText(
                "可以无损拼接 —— 不重新编码，画质零损失，几秒就完事。"
                if not warn_only else
                "可以无损拼接（有几点要注意，见下面）")
        else:
            self.banner_icon.setText("×")
            self.banner_text.setText("不建议剪辑 —— 下面这些地方必须重新编码，会很慢且画质有损。")

        n = len(plan.segments)
        size = estimate_size(plan)
        self.meta.setText(
            f"共 {n} 段，导出后约 {_fmt_dur(plan.total)}"
            + (f"，{_fmt_size(size)}" if size > 0 else "")
            + (f"（中间 {plan.gap:.1f} 秒空档会被去掉）" if plan.gap > 0.05 else ""))

        # 原因清单
        lay = self.body_lay
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        reasons = list(plan.reasons)
        if not reasons:
            row = QLabel("没有需要提醒的地方，直接导出就行。")
            row.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            lay.addWidget(row)
        for r in reasons:
            lay.addWidget(self._reason_row(r))
        lay.addStretch(1)

        # 首开时按条目多少定高: 只读模式已经没有底部按钮了, 留一大片空白很难看
        if not self._sized:
            self._sized = True
            need = 210 + sum(66 + 20 * len(r.items or []) for r in reasons)
            extra = 0 if self._readonly else 60
            self.resize(620, max(300, min(640, need + extra)))

        # 按钮
        if not self._readonly:
            self.go_btn.setEnabled(plan.exportable)
            if ok:
                self.go_btn.setText("导出")
                self.go_btn.setStyleSheet("")
                self.go_btn.setProperty("accent", True)
            else:
                self.go_btn.setText("仍要转码导出（慢 · 画质有损）")
                self.go_btn.setProperty("accent", False)
                self.go_btn.setStyleSheet(
                    f"QPushButton {{ background: {T.DANGER}; color: #FFFFFF;"
                    f" border: none; border-radius: 6px; padding: 6px 14px; }}"
                    f"QPushButton:hover {{ background: {T.DANGER}; opacity: 0.9; }}")
            self.go_btn.style().unpolish(self.go_btn)
            self.go_btn.style().polish(self.go_btn)
        if self.out_edit.text() != self.out_path:
            self.out_edit.setText(self.out_path)

    def _reason_row(self, r) -> QWidget:
        """一条原因: 图标 + 「故障 / 涉及谁 / 差在哪 / 会怎样」+ 修复按钮。

        用户的要求是**按可能出现的故障来说**, 并且关键的字加粗 ——
        所以这里用富文本拼: 故障名加粗、参数名加粗、后果用醒目的橙红色。
        """
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(9, 7, 9, 7)
        lay.setSpacing(9)
        w.setStyleSheet(
            f"background: {T.SURFACE_ALT}; border: 1px solid {T.BORDER};"
            f" border-radius: 8px;")
        ic = QLabel(_ICON.get(r.level, "i"))
        ic.setFixedWidth(16)
        ic.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        ic.setStyleSheet(f"color: {_COLOR.get(r.level, T.TEXT_SUB)};"
                         f" font-weight: 700; font-size: 13px;")
        lay.addWidget(ic)

        txt = QLabel(_reason_html(r))
        txt.setWordWrap(True)
        txt.setTextFormat(Qt.RichText)
        txt.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
        lay.addWidget(txt, 1)
        if r.fix and self._fix is not None:
            btn = QPushButton(r.fix_text or "修复")
            btn.setFixedHeight(26)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: {T.SURFACE}; border: 1px solid {T.BORDER};"
                f" border-radius: 6px; padding: 3px 10px; color: {T.TEXT}; font-size: 12px; }}"
                f"QPushButton:hover {{ border-color: {T.ACCENT}; color: {T.ACCENT}; }}")
            btn.clicked.connect(lambda _=False, c=r.fix: self._do_fix(c))
            lay.addWidget(btn)
        return w

    # ---------------------------------------------------------------- 动作
    def _do_fix(self, code: str):
        if self._fix is None:
            return
        try:
            changed = bool(self._fix(code))
        except Exception:
            changed = False
        self.fixed.emit(code)
        self.reload()

    def _pick(self):
        cur = self.out_path or ""
        d = os.path.dirname(cur) or os.path.expanduser("~")
        name = os.path.basename(cur) or "剪辑导出.mp4"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出到", os.path.join(d, name), "视频文件 (*.mp4)")
        if path:
            if not path.lower().endswith(".mp4"):
                path += ".mp4"
            self.out_path = path
            self.out_edit.setText(path)

    def _go(self):
        if not self.out_path.strip():
            self._pick()
            if not self.out_path.strip():
                return
        self.accept()
