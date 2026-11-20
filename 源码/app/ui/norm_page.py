# -*- coding: utf-8 -*-
"""统一格式 · 第三大形态(v206).

左主区 = 分组预览卡片(可展开组内明细); 右栏 = 结论条 + 参数目标 + 操作按钮.
与排序 / 合并共用同一套 splitter 骨架, 顶栏三按钮切换.

设计要点:
 - NormalizeDialog(弹窗, v200-v204) 保持原样不动, 老验收照旧通过;
   本模块是"大形态"的新载体, 两者共用 normalize_dialog 里的可复用组件.
 - NormWorkspace 是协调者(非 QWidget): 持有 photos/specs/plan, 参数变化重算 plan,
   同时刷新预览卡片与结论条; 只对外抛 startRequested(profile, plan) 给主窗口执行.
"""
from __future__ import annotations

import copy
import html
import os

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QStyle, QStyleOptionComboBox,
                               QVBoxLayout, QWidget)

from . import theme as T
from . import compare_model
from .dialogs import _CenterAlignDelegate
from .edit_icons import make_icon
from .merge_panel import _PreviewLabel, _sep
from .normalize_dialog import _GroupCard, _DownComboBox, _banner_style
from ..merge import normalize, settings


# ---------------------------------------------------------------- v242 参数表
# 13 项"必须一致"参数的呈现定义(单一来源)。kind:
#   combo = 有可调目标控件; derived = 由编码档推导(只读); fixed = 目标唯一(只读)
#   group: picture=画面类 / audio=音频 / container=流结构 / codec=视频编码
NORM_PARAM_ROWS = [
    # key,            名称,          等级,   kind,     group,    说明
    ("codec",         "编码",        "fatal", "combo",  "codec",  "决定像素格式与位深；H.264 兼容 / H.265 均衡 / AV1 省空间"),
    ("pix_fmt",       "像素格式",    "fatal", "derived", "codec",  "由视频编码档决定，切换编码档即时更新"),
    ("bits",          "位深",        "fatal", "derived", "codec",  "由视频编码档决定，切换编码档即时更新"),
    ("width",         "分辨率",      "fatal", "combo",  "picture", "可设目标分辨率：选 keep 保持原样并分组隔离；选具体值则缩放重渲染"),
    ("r_frame_rate",  "帧率",        "fatal", "combo",  "picture", "可设目标帧率：选具体值则插/抽帧重渲染"),
    ("color_transfer","HDR 传输",    "fatal", "combo",  "picture", "可设目标色彩：SDR / HLG / HDR10，选具体值则色调映射重渲染"),
    ("audio_codec",   "编码",        "fatal", "combo",  "audio",  "可选 AAC-LC / AC-3 杜比 / ALAC 苹果无损；mp4 实测仅这三种可封装"),
    ("sample_fmt",    "采样格式",    "fatal", "derived","audio",  "由音频编码档决定：AAC/AC-3 为 fltp，ALAC 无损为 s16p"),
    ("sample_rate",   "采样率",      "fatal", "fixed",  "audio",  "固定 48kHz（目标唯一）"),
    ("channels",      "声道数",      "fatal", "fixed",  "audio",  "按整批素材自动判定：全单声道则保留单声道，否则统一立体声"),
    ("nb_streams",    "流结构",      "fatal", "fixed",  "container", "统一为 1 路视频 + 1 路音频（丢弃多余音轨/流）"),
    ("rotation",      "旋转",        "fatal", "fixed",  "container", "统一归零（旋转信息烘进画面）"),
    ("field_order",   "扫描方式",    "fatal", "fixed",  "container", "统一逐行（隔行转逐行）"),
]

# v245: 只读项说明的单行短版(原文案留在 NORM_PARAM_ROWS, 鼠标悬停可见)。
# 弹窗一行两列, 说明区窄——长文案会折成 2-3 行, 短版本保证一行内显示完整。
# v258: pix_fmt/bits 改成按当前编码档显示确定值(不再写模糊的 8/10bit 文案),
# 下拉切换时实时刷新; 这两项初始值仍给占位, 由 _refresh_codec_derived() 填。
_NORM_DESC_SHORT = {
    "pix_fmt":     "随编码档确定",
    "bits":        "随编码档确定",
    "sample_fmt":  "随音频编码档确定",
    "sample_rate": "48kHz",
    "channels":    "随素材单/立体声",
    "nb_streams":  "单视频+单音频",
    "rotation":    "归零，烘进画面",
    "field_order": "逐行",
}

# v243: 弹窗按 4 类重组(单一来源)。色条 = 类别识别色; 顶栏三个档位并入对应类别。
_NORM_GROUPS = [
    ("codec",     "编码",     "决定像素格式与位深，改档需要重新编码", T.ACCENT),
    ("picture",   "画面目标", "设具体值将缩放 / 插帧 / 色调映射，耗时更长", T.WARN),
    ("audio",     "音频",     "编码与码率可调，采样格式随编码档确定", T.OK),
    ("container", "流结构",   "统一为单视频单音频、去旋转、逐行", T.TEXT_SUB),
]
_EXTRA_GROUP = {"keyint_sec": "codec", "crf": "codec", "audio_bitrate": "audio"}

# combo 行 key → 对应 profile 字段(可调目标)。外露勾选框只给这些 key。
_NORM_COMBO_KEYS = {"codec", "width", "r_frame_rate", "color_transfer",
                    "audio_bitrate", "keyint_sec", "crf"}
# 13 项表的 key → profile/settings 字段 key(画面目标命名不同)
_NORM_KEY_TO_PROFILE = {"width": "target_res", "r_frame_rate": "target_fps",
                        "color_transfer": "target_color"}

_LEVEL_COLOR = {"fatal": T.DANGER, "warn": T.WARN, None: T.TEXT_WEAK}
_LEVEL_TEXT = {"fatal": "必须一致", "warn": "建议一致", None: "仅供参考"}


class NormPreview(QWidget):
    """左主区: 分组预览(标题 + 卡片滚动区 + 底部说明)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("normPreview")
        self._build()
        self.set_plan(None)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 10)
        root.setSpacing(5)
        cap = QLabel("分组预览")
        cap.setObjectName("normPreviewTitle")
        cap.setStyleSheet(f"color: {T.TEXT}; font-size: 13px; font-weight: 600;")
        root.addWidget(cap)
        # v207: 去掉「按合并组展示 · 点卡片展开组内文件明细」提示文字

        self.group_area = QScrollArea()
        self.group_area.setObjectName("normGroupArea")
        self.group_area.setWidgetResizable(True)
        self.group_area.setFrameShape(QFrame.NoFrame)
        self.group_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.group_area.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.group_host = QWidget()
        self.group_lay = QVBoxLayout(self.group_host)
        self.group_lay.setContentsMargins(0, 4, 4, 0)
        self.group_lay.setSpacing(8)
        self.group_area.setWidget(self.group_host)
        root.addWidget(self.group_area, 1)

        tip = QLabel("原文件不动；副本带参数指纹，源变化自动重建。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        root.addWidget(tip)

    def set_plan(self, plan):
        while self.group_lay.count():
            it = self.group_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if plan is not None:
            for g in (plan.groups or []):
                self.group_lay.addWidget(_GroupCard(g))
            if plan.errors and not plan.groups:
                lab = QLabel("；".join(plan.errors[:3]))
                lab.setWordWrap(True)
                lab.setStyleSheet(f"color: {T.DANGER}; font-size: 12px;")
                self.group_lay.addWidget(lab)
            elif not plan.groups:
                lab = QLabel("没有可统一的文件")
                lab.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
                self.group_lay.addWidget(lab)
        else:
            lab = QLabel("还没有视频可统一，点顶栏「添加」把视频加进来。")
            lab.setWordWrap(True)
            lab.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            self.group_lay.addWidget(lab)
        self.group_lay.addStretch(1)

    def cards(self):
        """给验收/调试: 当前卡片列表。"""
        return [self.group_lay.itemAt(i).widget()
                for i in range(self.group_lay.count())
                if hasattr(self.group_lay.itemAt(i).widget(), "toggle_expanded")]


class NormParamSlot(QWidget):
    """统一页顶栏的参数槽(v228)。

    顶栏挤压时**按比例整体缩放** 4 个下拉(非可编辑下拉自动省略当前文字),
    绝不把右侧的 编码/分辨率/帧率 开关叠到参数条上(v211 时代 min/max 全锁死,
    顶栏分不到空间就会出现用户截图的"按钮重叠"); 宽度富余时各下拉精确回到
    fit 宽度(fit_combo_width 算出的"最长选项 + 装饰"), 文字完整显示。
    """
    FLOOR = 56               # 压缩下限: 每格最少留 56px(再窄就宁可挤出槽)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cells: list[tuple[QComboBox, int]] = []

    def register(self, cells):
        """主窗口把 (combo, fit_width) 列表交进来(宽度来源 = NormPanel.cell)。"""
        self._cells = [(cb, int(w)) for cb, w in cells]
        self._apply(self.width())

    def minimumSizeHint(self) -> QSize:
        n = max(1, len(self._cells))
        from PySide6.QtCore import QSize as _QS
        return _QS(n * self.FLOOR + 10 * (n - 1),
                   super().minimumSizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply(e.size().width())

    def _apply(self, width: int):
        if not self._cells:
            return
        total = sum(w for _, w in self._cells)
        gap = 10                                    # NormPanel param_bar 的间距
        avail = width - gap * (len(self._cells) - 1)
        if avail >= total:
            for cb, w in self._cells:
                cb.setFixedWidth(w)
            return
        scale = max(0.0, avail) / total
        for cb, w in self._cells:
            cb.setFixedWidth(max(self.FLOOR, round(w * scale)))


def _expose_icon(on: bool) -> QIcon:
    """v243: 画出来的对勾/空框, 不依赖字形(本机 ✅ 字符会渲染成空方框)。"""
    n = 16
    pm = QPixmap(n, n)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    box = QRectF(1.5, 1.5, 13, 13)
    if on:
        p.setBrush(QColor(T.OK))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QPen(Qt.white, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(QPointF(4.4, 8.2), QPointF(6.9, 10.6))
        p.drawLine(QPointF(6.9, 10.6), QPointF(11.6, 5.5))
    else:
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(T.TEXT_WEAK), 1.4))
        p.drawRoundedRect(box, 3, 3)
    p.end()
    return QIcon(pm)


class ExposeToggle(QPushButton):
    """v245: 外露开关 —— 只显示一个自绘对勾/空框图标, 不带文字。

    对勾用 QPainter 画出来(不用 ✅ 字符, 本机字体缺该字形会变空方框)。
    保留 QPushButton.checkable 的标准接口(isChecked / setChecked / toggled),
    旧逻辑与验收脚本按 QCheckBox 的调用方式无需改动。
    """

    WIDTH = 28

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(self.WIDTH)
        self.setToolTip("外露到顶栏参数条")
        self._icon_on = _expose_icon(True)
        self._icon_off = _expose_icon(False)
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                padding: 0px;
            }}
        """)
        self.toggled.connect(self._sync_icon)
        self._sync_icon(self.isChecked())

    def _sync_icon(self, on: bool):
        self.setIcon(self._icon_on if on else self._icon_off)


class NormParamsDialog(QDialog):
    """v243 统一参数配置弹窗: 13 项门槛 + 3 项档位按 4 类分组, ✅ 外露开关。

    可调项给下拉, 推导/固定项只读并标「推导」「固定」; 每项左侧的 ☐/✅ 胶囊
    控制是否外露到顶栏。保存为默认 / 恢复默认合并进来。
    v246: 外露勾选实时生效(只写外露配置 + exposureChanged 刷新顶栏, 不动档位);
    「保存」提交档位+外露; 没点保存直接关窗(X/ESC)则把外露回滚到进窗快照。
    """
    applied = Signal()           # 档位+外露整体提交(点保存/存默认)
    exposureChanged = Signal()   # v246: 仅外露变化, 用来实时刷新顶栏参数条

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("统一参数配置")
        self.setMinimumSize(680, 720)
        self._expose_boxes: dict[str, ExposeToggle] = {}
        self._target_combos: dict[str, QComboBox] = {}
        self._derived_labels: dict[str, QLabel] = {}  # v258: 推导项值标签(随编码档刷新)
        self._group_rows: dict[str, list[str]] = {}   # v243: 分组归属(验收/调试)
        self._saved = False                 # v246: 是否已点「保存」提交
        self._orig_exposed = list(settings.load_norm_exposed())  # v246: 进窗外露快照
        self._loading = True

        root = QVBoxLayout(self)
        root.setSpacing(10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        grid = QVBoxLayout(body)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setSpacing(12)   # v247: 四个分类卡片之间拉开距离, 彼此分开

        prof = normalize.NormProfile.from_dict()
        exposed = set(settings.load_norm_exposed())
        # profile 字段 → 当前值(画面目标用 settings 清洗后的 dict)
        saved = settings._clean_normalize_profile(prof.to_dict())

        # v243: 16 项按类别归拢 —— 13 项门槛表 + 3 项顶栏档位(并入对应类别)
        items = {g: [] for g, _, _, _ in _NORM_GROUPS}
        for key, name, level, kind, group, desc in NORM_PARAM_ROWS:
            items.setdefault(group, []).append({
                "key": key, "name": name, "level": level, "kind": kind,
                "desc": desc, "combo": self._make_control(key, kind, saved),
            })
        for key, name, combo in self._extra_controls(saved):
            g = _EXTRA_GROUP.get(key, "codec")
            items.setdefault(g, []).append({
                "key": key, "name": name, "level": None, "kind": "combo",
                "desc": "顶栏画质 / 音质档位，非合并门槛",
                "combo": combo, "extra": True,
            })

        # v247: 每个分类(标题 + 参数行)整体装进一个独立圆角卡片, 四类彼此分开
        for gkey, gname, gdesc, gcolor in _NORM_GROUPS:
            rows = items.get(gkey) or []
            if not rows:
                continue
            # v260: 音频组按用户指定顺序重排 —— 首行 编码|码率|采样格式, 次行 采样率|声道数
            if gkey == "audio":
                _order = ["audio_codec", "audio_bitrate", "sample_fmt",
                          "sample_rate", "channels"]
                rows = sorted(rows, key=lambda r: _order.index(r["key"])
                              if r["key"] in _order else len(_order))
            self._group_rows[gkey] = [r["key"] for r in rows]

            card = QFrame()
            card.setObjectName("normGroupCard")
            card.setStyleSheet(f"""
                QFrame#normGroupCard {{
                    background: {T.SURFACE};
                    border: 1px solid {T.BORDER};
                    border-radius: 8px;
                }}
            """)
            cv = QVBoxLayout(card)
            cv.setContentsMargins(8, 8, 8, 8)
            cv.setSpacing(5)

            sec = QFrame()
            sec.setObjectName("normParamSection")
            sec.setStyleSheet(f"""
                QFrame#normParamSection {{
                    background: {T.HOVER_SOFT};
                    border: none;
                    border-left: 3px solid {gcolor};
                    border-radius: 3px;
                }}
            """)
            sl = QHBoxLayout(sec)
            sl.setContentsMargins(8, 5, 6, 5)
            sl.setSpacing(6)
            title = QLabel(gname)
            title.setStyleSheet(f"color: {gcolor}; font-size: 12px; font-weight: 700;")
            sl.addWidget(title)
            sub = QLabel(gdesc)
            sub.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 11px;")
            sl.addWidget(sub, 1)
            cnt = QLabel(f"{len(rows)} 项")
            cnt.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 11px;")
            sl.addWidget(cnt)
            cv.addWidget(sec)

            # v244: 一行两列(左/右各一项), 下拉按内容收窄, 不再拉伸占满整行
            # v259: 编码组首行三项(视频编码/像素格式/位深)塞进同一行, 一眼看清对应关系;
            #       其余组/其余行维持两列。
            if gkey in ("codec", "audio") and len(rows) >= 3:
                row_plan = [rows[:3]] + [rows[i:i + 2] for i in range(3, len(rows), 2)]
            else:
                row_plan = [rows[i:i + 2] for i in range(0, len(rows), 2)]
            for row_i, chunk in enumerate(row_plan):
                triple = len(chunk) == 3
                line = QFrame()
                line.setObjectName("normParamRow")
                bg = T.SURFACE if row_i % 2 == 0 else "#FAFBFD"
                line.setStyleSheet(
                    f"QFrame#normParamRow {{ background: {bg}; border: 1px solid {T.BORDER}; border-radius: 4px; }}")
                lh = QHBoxLayout(line)
                lh.setContentsMargins(8, 5, 8, 5)
                lh.setSpacing(5 if triple else 8)

                for it in chunk:
                    cell = QWidget()
                    h = QHBoxLayout(cell)
                    h.setContentsMargins(0, 0, 0, 0)
                    h.setSpacing(4 if triple else 6)

                    # 外露开关(只给可调项); 不可外露项留同宽占位, 让名称列对齐
                    # v259: 首行三列时派生项(pix_fmt/bits)无勾选框, 去掉占位省宽给编码下拉
                    profile_key = _NORM_KEY_TO_PROFILE.get(it["key"], it["key"])
                    if it["key"] in _NORM_COMBO_KEYS and profile_key in settings.NORM_EXPOSABLE:
                        cb_ex = ExposeToggle()
                        # v244: settings 存的是 profile key(target_res/target_fps/target_color),
                        # 表里的 it["key"] 是 width/r_frame_rate/color_transfer —— 用后者
                        # 比对会永远勾不上, 重开弹窗显示"没选中但已外露"。
                        cb_ex.setChecked(profile_key in exposed)
                        # v246: 外露勾选实时生效 —— 只写外露配置并刷新顶栏, 不动档位
                        cb_ex.toggled.connect(self._on_exposure_toggled)
                        self._expose_boxes[it["key"]] = cb_ex
                        h.addWidget(cb_ex)
                    elif not triple:
                        blank = QLabel("")
                        blank.setFixedWidth(ExposeToggle.WIDTH)
                        h.addWidget(blank)

                    # 名称 + 等级色(v244: 用户要求去掉 ★ 前缀)
                    lab = QLabel(it["name"])
                    lab.setMinimumWidth(56 if triple else 72)
                    color = T.TEXT_SUB if it.get("extra") else _LEVEL_COLOR.get(it["level"], T.TEXT_WEAK)
                    lab.setStyleSheet(f"color: {color}; font-weight: 600;")
                    h.addWidget(lab)

                    # 控件 / 只读说明 + 推导/固定徽标
                    if it["combo"] is not None:
                        cb = it["combo"]
                        cb.setSizeAdjustPolicy(QComboBox.AdjustToContents)
                        cb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                        h.addWidget(cb)
                        self._target_combos[it["key"]] = cb
                    else:
                        # v245: 用单行短文案(原文案进 tooltip), 保证一行内显示完整不折行
                        short = _NORM_DESC_SHORT.get(it["key"], it["desc"])
                        dv = QLabel(short)
                        dv.setProperty("muted", True)
                        dv.setToolTip(it["desc"])
                        h.addWidget(dv, 1)
                        if it["key"] in ("pix_fmt", "bits", "sample_fmt"):
                            self._derived_labels[it["key"]] = dv
                    # v259: 首行三列按内容宽度分配(而非均分), 保证编码下拉不被挤压截断
                    lh.addWidget(cell, cell.sizeHint().width() if triple else 1)

                if len(chunk) == 1:
                    lh.addStretch(1)   # 奇数最后一项: 右列留空
                cv.addWidget(line)

            grid.addWidget(card)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self.tip_label = QLabel("")
        self.tip_label.setStyleSheet(f"color: {T.OK}; font-size: 12px;")
        root.addWidget(self.tip_label)

        btns = QHBoxLayout()
        b_default = QPushButton("保存为默认")
        b_default.setToolTip("把当前这套参数存为基准档")
        b_default.clicked.connect(self._save_default)
        btns.addWidget(b_default)
        b_restore = QPushButton("恢复默认")
        b_restore.setToolTip("回到出厂参数（画面目标全部 keep）")
        b_restore.clicked.connect(self._restore_default)
        btns.addWidget(b_restore)
        btns.addStretch(1)
        done = QPushButton("保存")
        done.setProperty("accent", True)
        done.setMinimumWidth(88)
        done.clicked.connect(self._apply)
        btns.addWidget(done)
        root.addLayout(btns)

        self._loading = False
        # v258: 编码档切换 → 像素格式/位深显示确定值(不再模糊的 8/10bit)
        codec_combo = self._target_combos.get("codec")
        if codec_combo is not None:
            codec_combo.currentIndexChanged.connect(self._refresh_codec_derived)
        # v260: 音频编码切换 → 采样格式显示确定值(s16p/fltp) + ALAC 时码率置灰
        acb = self._target_combos.get("audio_codec")
        if acb is not None:
            acb.currentIndexChanged.connect(self._refresh_audio_derived)
        self._refresh_codec_derived()
        self._refresh_audio_derived()
        self.resize(720, 800)

    def _refresh_codec_derived(self, *_):
        """v258: 按当前编码档, 把像素格式/位深两行刷新成确定值。"""
        combo = self._target_combos.get("codec")
        if combo is None:
            return
        code = str(combo.currentData() or "main")
        spec = normalize.CODEC_SPECS.get(code) or normalize.CODEC_SPECS["main"]
        pf = self._derived_labels.get("pix_fmt")
        if pf is not None:
            pf.setText(spec["pix_fmt"])
        bt = self._derived_labels.get("bits")
        if bt is not None:
            bt.setText(f"{spec['bits']}bit")

    def _refresh_audio_derived(self, *_):
        """v260: 按当前音频编码档刷新采样格式显示, 无损 ALAC 时把码率下拉置灰。"""
        combo = self._target_combos.get("audio_codec")
        if combo is None:
            return
        code = str(combo.currentData() or "aac")
        spec = normalize.AUDIO_CODEC_SPECS.get(code) or normalize.AUDIO_CODEC_SPECS["aac"]
        sf = self._derived_labels.get("sample_fmt")
        if sf is not None:
            sf.setText(spec["sample_fmt"])
        br = self._target_combos.get("audio_bitrate")
        if br is not None:
            lossless = bool(spec["lossless"])
            br.setEnabled(not lossless)
            br.setToolTip("无损编码，码率不适用" if lossless else "")

    def _make_control(self, key, kind, saved):
        """给画面类目标生成下拉; 其它 combo 行(如 codec)返回 None 交给 extra? 不,
        codec 也在 13 项里, 这里生成它, 避免重复。"""
        if kind != "combo":
            return None
        combo = _DownComboBox()
        if key == "codec":
            for k, label in normalize.CODEC_LABELS.items():
                combo.addItem(label, k)
            combo.setCurrentIndex(max(0, list(normalize.CODEC_LABELS).index(saved["codec"])))
        elif key == "audio_codec":
            # v260: 音频编码下拉(AAC / AC-3 / ALAC), 文案来自规格表单来源
            for k, label in normalize.AUDIO_CODEC_LABELS.items():
                combo.addItem(label, k)
            combo.setCurrentIndex(max(0, list(normalize.AUDIO_CODEC_LABELS).index(saved["audio_codec"])))
        elif key == "width":
            combo.addItem("不修改，分组保存", "keep")
            for r in settings.TARGET_RES_CHOICES:
                combo.addItem(r, r)
            combo.setCurrentIndex(max(0, [combo.itemData(i) for i in range(combo.count())].index(saved["target_res"])))
        elif key == "r_frame_rate":
            combo.addItem("不修改，分组保存", "keep")
            for f in settings.TARGET_FPS_CHOICES:
                combo.addItem(f"{f} fps", f)
            combo.setCurrentIndex(max(0, [combo.itemData(i) for i in range(combo.count())].index(saved["target_fps"])))
        elif key == "color_transfer":
            labels = {"keep": "不修改，分组保存", "sdr": "SDR（BT.709）",
                      "hlg": "HLG", "hdr10": "HDR10"}
            for k in ("keep",) + settings.TARGET_COLOR_CHOICES:
                combo.addItem(labels.get(k, k), k)
            combo.setCurrentIndex(max(0, [combo.itemData(i) for i in range(combo.count())].index(saved["target_color"])))
        else:
            return None
        return combo

    def _extra_controls(self, saved):
        """音频码率 / 关键帧 / 码率质量三个顶栏档位控件。"""
        out = []
        cb = _DownComboBox()
        for v in normalize.AUDIO_BITRATE_CHOICES:
            cb.addItem(normalize.audio_bitrate_label(v), v)
        cb.setCurrentIndex(max(0, normalize.AUDIO_BITRATE_CHOICES.index(saved["audio_bitrate"])
                              if saved["audio_bitrate"] in normalize.AUDIO_BITRATE_CHOICES else 1))
        out.append(("audio_bitrate", "码率", cb))
        cb = _DownComboBox()
        for v in normalize.KEYINT_CHOICES:
            cb.addItem(f"{v} 秒" + ("（推荐）" if v == 2 else ""), v)
        cb.setCurrentIndex(max(0, normalize.KEYINT_CHOICES.index(saved["keyint_sec"])))
        out.append(("keyint_sec", "关键帧间隔", cb))
        cb = _DownComboBox()
        for v in normalize.CRF_CHOICES:
            cb.addItem(f"{v}" + ("（高质量）" if v == 18 else "（均衡）" if v == 20 else "（省空间）"), v)
        cb.setCurrentIndex(max(0, normalize.CRF_CHOICES.index(saved["crf"])))
        out.append(("crf", "码率质量", cb))
        return out

    def _profile_from_ui(self) -> dict:
        def data(key, fallback):
            combo = self._target_combos.get(key)
            if combo is None:
                return fallback
            return combo.currentData()
        return {
            "codec": data("codec", "main"),
            "audio_bitrate": int(data("audio_bitrate", 192) or 192),
            "audio_codec": data("audio_codec", "aac"),
            "keyint_sec": int(data("keyint_sec", 2) or 2),
            "crf": int(data("crf", 20) or 20),
            "target_res": data("width", "keep"),
            "target_fps": data("r_frame_rate", "keep"),
            "target_color": data("color_transfer", "keep"),
        }

    def _on_exposure_toggled(self, _checked=0):
        """v246: 外露勾选实时生效 —— 只写外露配置, 不碰档位, 通知顶栏刷新。

        存盘前 config 自带 .bak 备份; 没点保存而关窗时 closeEvent 会回滚到快照。
        """
        if self._loading:
            return
        settings.save_norm_exposed(self._exposed_from_ui())
        self.exposureChanged.emit()

    def closeEvent(self, ev):
        """v246: 没点「保存」就关窗(X/ESC/系统关闭) → 外露回滚到进窗快照。"""
        if not self._saved:
            settings.save_norm_exposed(self._orig_exposed)
            self.exposureChanged.emit()
        super().closeEvent(ev)

    def _exposed_from_ui(self) -> list:
        # 13 项表里的 key(width/r_frame_rate/color_transfer) → profile 字段 key
        box_key = {"target_res": "width", "target_fps": "r_frame_rate",
                   "target_color": "color_transfer"}
        out = []
        for k in settings.NORM_EXPOSABLE:
            bk = box_key.get(k, k)
            box = self._expose_boxes.get(bk)
            if box is not None and box.isChecked():
                out.append(k)
        return out

    def _apply(self):
        prof = self._profile_from_ui()
        settings.save_normalize_profile_current(prof)
        ok_exp = settings.save_norm_exposed(self._exposed_from_ui())
        self._saved = True      # v246: 已提交, closeEvent 不再回滚外露
        self.applied.emit()
        self.accept()

    def _save_default(self):
        prof = self._profile_from_ui()
        ok = settings.save_normalize_profile(prof)
        settings.save_normalize_profile_current(prof)
        exp = self._exposed_from_ui()
        settings.save_norm_exposed(exp)
        # v246: 「保存为默认」已把外露落盘, 更新快照并标记提交, 关窗不再回滚掉它
        self._orig_exposed = list(exp)
        self._saved = True
        self.tip_label.setText("已保存为默认" if ok else "保存失败")
        self.applied.emit()

    def _restore_default(self):
        d = dict(settings.DEFAULT_NORMALIZE_PROFILE)
        # 把 combo 全部拨回出厂值
        mapping = {"codec": d["codec"], "width": d["target_res"],
                   "r_frame_rate": d["target_fps"], "color_transfer": d["target_color"],
                   "audio_bitrate": d["audio_bitrate"], "keyint_sec": d["keyint_sec"],
                   "crf": d["crf"]}
        for key, val in mapping.items():
            combo = self._target_combos.get(key)
            if combo is None:
                continue
            idx = [combo.itemData(i) for i in range(combo.count())]
            if val in idx:
                combo.setCurrentIndex(idx.index(val))
        box_key = {"target_res": "width", "target_fps": "r_frame_rate",
                   "target_color": "color_transfer"}
        default_box_keys = {box_key.get(k, k) for k in settings.DEFAULT_NORM_EXPOSED}
        for bk, box in self._expose_boxes.items():
            box.setChecked(bk in default_box_keys)
        self.tip_label.setText("已恢复出厂参数（外露实时生效，点保存提交档位）")


class NormPanel(QWidget):
    """右栏(v207 精简): 结论条 + 保存默认 / 开始统一 + 图例。

    v207: 4 个可调下拉搬到顶栏「添加」右侧(用户要求), 但仍归 NormPanel 持有
    (profile()/profileChanged 链路不变), 由主窗口 reparent 到顶栏的参数容器。
    右栏只留结论、按钮、图例 —— 参数目标不再占右栏。
    """

    profileChanged = Signal()
    saveRequested = Signal()
    startRequested = Signal()
    revealRequested = Signal()     # v209: 底部「打开位置」
    outDirChanged = Signal(str)    # v234: 用户在「选择…」里改了副本输出根目录
    editRequested = Signal()       # v209: 底部「剪辑」
    previewRequested = Signal()    # v209: 点预览图打开源文件
    paramsRequested = Signal()     # v242: ⚙ 点开参数配置弹窗

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("normPanel")
        self._param_cells: list[tuple[QComboBox, int]] = []   # (combo, fit 宽)
        self._build()
        # v234: 回填用户上次选的副本根目录(空=跟源视频同目录)
        try:
            from ..merge import settings as _st
            self.dir_edit.setText(_st.load_normalize_out_dir())
        except Exception:
            pass

    def _pick_dir(self):
        """v234: 选副本输出根目录 —— 与合并页「选择…」同款交互。"""
        from PySide6.QtWidgets import QFileDialog
        start = self.dir_edit.text().strip() or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "选择统一副本保存位置", start)
        if not path:
            return
        self.dir_edit.setText(path)
        from ..merge import settings as _st
        _st.save_normalize_out_dir(path)
        self.outDirChanged.emit(path)

    def _build(self):
        self._profile_ready = False
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # v254: 结论条移到对比清单「必须一致」红标题正上方 —— 纯文字两行,
        # 去掉原来的浅绿圆角框和 √ 图标, 不额外占空间。此处只建控件,
        # 实际 add 位置在 compare_scroll 之前(见下方)。
        self.banner = QFrame()
        self.banner.setObjectName("normBanner")
        self.banner.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(0, 1, 0, 1)
        bl.setSpacing(0)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        self.banner_text.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {T.TEXT};")
        bl.addWidget(self.banner_text, 1)

        # v234: 仿合并右栏 —— 保存位置行 = 标签 + 输入框(显示副本根目录) + 「选择…」按钮。
        # 选中的目录作为统一副本的输出根(副本放其下 _photosort_norm/); 留空=跟源视频
        # 同目录。路径由 norm_paths.work_path 统一解析, 转码/读取/清理一致。
        loc = QHBoxLayout()
        loc.setSpacing(8)
        title = QLabel("保存位置")
        title.setStyleSheet(f"color: {T.TEXT}; font-size: 14px; font-weight: 600;")
        loc.addWidget(title)
        self.dir_edit = QLineEdit()
        self.dir_edit.setReadOnly(True)
        self.dir_edit.setPlaceholderText("默认与源视频同目录（可点右侧「选择…」指定）")
        loc.addWidget(self.dir_edit, 1)
        self.dir_btn = QPushButton("选择…")
        self.dir_btn.setFixedWidth(64)
        self.dir_btn.clicked.connect(self._pick_dir)
        loc.addWidget(self.dir_btn)
        root.addLayout(loc)

        self.preview = _PreviewLabel()
        self.preview.clicked.connect(self.previewRequested)
        root.addWidget(self.preview)

        # v253: 与合并右栏同款摘要「已选 N 段 · 总时长 … · 约 … MB」
        self.summary = QLabel("还没有勾选视频")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
        root.addWidget(self.summary)

        # v251: 删掉「参数对比」标题文字与其上方的横线, 对比清单直接紧接预览图显示。
        # v254: 原顶部空 hint 行删除 —— 结论条移到此处(对比清单红标题正上方)。
        self.banner.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        root.addWidget(self.banner)
        self.compare_scroll = QScrollArea()
        self.compare_scroll.setWidgetResizable(True)
        self.compare_scroll.setFrameShape(QFrame.NoFrame)
        self.compare_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.compare_scroll.setMinimumHeight(100)
        self.compare_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.compare_body = QLabel()
        self.compare_body.setWordWrap(True)
        self.compare_body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.compare_body.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
        # v255: ⓘ 角标立即弹说明窗(绕开富文本 <a title> tooltip 不弹/延迟)
        self._info_filter = compare_model.InfoPopupFilter(self.compare_body)
        self.compare_scroll.setWidget(self.compare_body)
        root.addWidget(self.compare_scroll, 1)

        # v207: 4 个可调下拉组成独立横排参数栏, 由主窗口 reparent 到顶栏「添加」右侧。
        # 仍是本 panel 持有(profile()/profileChanged 链路不变), 只是显示位置在顶栏。
        self.param_bar = QFrame()
        self.param_bar.setObjectName("normParamBar")
        pbl = QHBoxLayout(self.param_bar)
        pbl.setContentsMargins(0, 0, 0, 0)
        pbl.setSpacing(10)

        prof = normalize.NormProfile.from_dict()

        def fit_combo_width(combo, extra: int = 2, probe: int = 400) -> int:
            """刚好放得下最长选项的宽度(v213)。

            装饰(左右内边距 + 边框 + 箭头区)不写死: 拿一个假想宽 probe 让样式算一次
            "文字区"宽度, 差出来的就是装饰 —— 以后改 QSS 的 padding / ::drop-down
            宽度, 这里会自动跟着变, 不用再手改 236/200 这些数字。
            """
            fm = combo.fontMetrics()
            need = max((fm.horizontalAdvance(combo.itemText(i))
                        for i in range(combo.count())), default=0)
            opt = QStyleOptionComboBox()
            opt.initFrom(combo)
            opt.rect = QRect(0, 0, probe, max(24, combo.sizeHint().height()))
            opt.currentText = combo.currentText()
            opt.editable = False
            edit = combo.style().subControlRect(
                QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, combo)
            return need + (probe - edit.width()) + extra

        def cell(name, color, combo, width):
            w = QWidget()
            # v228: 每格宽度仍按 fit_combo_width 锁定(文字完整显示), 但槽本体
            # (NormParamSlot)会在顶栏挤的时候按比例整体压缩各下拉 —— 以前只锁
            # 格子不锁槽的伸缩, 顶栏分不到空间时 Qt 会把右侧开关/滑块直接叠到
            # 参数条上(用户截图的"按钮重叠")。
            w.setFixedWidth(width)
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(2)
            lab = QLabel(name)
            lab.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600;")
            v.addWidget(lab)
            combo.setFixedWidth(width)
            combo.setMinimumHeight(26)
            v.addWidget(combo)
            self._param_cells.append((combo, width))
            pbl.addWidget(w)
            return w

        # v208: 弹出菜单样式对齐排序页 view_mode_combo —— 选中项浅蓝圆角底 + 右侧对勾;
        # 开启 mouseTracking 否则鼠标划过时画不出 hover 高亮。
        def fancy(combo):
            combo.view().setMouseTracking(True)
            combo.setItemDelegate(_CenterAlignDelegate(combo))
            # v228: 参数格被顶栏压缩时, 弹出菜单仍按"最长选项"给足宽度,
            # 下拉里的选项文字不被省略(顶栏上当前值可以省, 菜单里不行)。
            try:
                combo.view().setMinimumWidth(combo.sizeHint().width())
            except Exception:
                pass

        self.codec_combo = _DownComboBox()
        for key, label in normalize.CODEC_LABELS.items():
            self.codec_combo.addItem(label, key)
        self.codec_combo.setCurrentIndex(
            max(0, list(normalize.CODEC_LABELS).index(prof.codec)))
        fancy(self.codec_combo)

        self.audio_combo = _DownComboBox()
        for v in normalize.AUDIO_BITRATE_CHOICES:
            self.audio_combo.addItem(normalize.audio_bitrate_label(v), v)
        self.audio_combo.setCurrentIndex(
            max(0, normalize.AUDIO_BITRATE_CHOICES.index(prof.audio_bitrate)
                if prof.audio_bitrate in normalize.AUDIO_BITRATE_CHOICES else 1))
        fancy(self.audio_combo)

        self.keyint_combo = _DownComboBox()
        for v in normalize.KEYINT_CHOICES:
            self.keyint_combo.addItem(f"{v} 秒" + ("（推荐）" if v == 2 else ""), v)
        self.keyint_combo.setCurrentIndex(
            max(0, normalize.KEYINT_CHOICES.index(prof.keyint_sec)))
        fancy(self.keyint_combo)

        self.crf_combo = _DownComboBox()
        for v in normalize.CRF_CHOICES:
            self.crf_combo.addItem(f"{v}" + ("（高质量）" if v == 18 else
                                             "（均衡）" if v == 20 else "（省空间）"), v)
        self.crf_combo.setCurrentIndex(max(0, normalize.CRF_CHOICES.index(prof.crf)))
        fancy(self.crf_combo)

        # 顺序: 编码 / 音频(必须一致,红) → 关键帧(建议,橙) → 码率(参考,灰)
        # v208: 宽度按最长文案给足, 关闭态文字完整显示(不再 elide 成 "…")。
        # v213: 宽度不再写死 236/200/116/120 —— 按每个下拉"最长选项 + 装饰"现算,
        #       既有 5px 放不下的(keyint/crf 原本就差一点)会补足, 原本空一大截的
        #       (codec/audio)会收窄; 参数栏也随之短一截。
        # v242: 画面目标 combo(与弹窗同源选项)。始终创建, 是否显示由外露列表决定。
        self.target_res_combo = _DownComboBox()
        self.target_res_combo.addItem("不修改，分组保存", "keep")
        for r in settings.TARGET_RES_CHOICES:
            self.target_res_combo.addItem(r, r)
        self.target_res_combo.setCurrentIndex(max(0, [self.target_res_combo.itemData(i)
            for i in range(self.target_res_combo.count())].index(prof.target_res)))
        fancy(self.target_res_combo)

        self.target_fps_combo = _DownComboBox()
        self.target_fps_combo.addItem("不修改，分组保存", "keep")
        for f in settings.TARGET_FPS_CHOICES:
            self.target_fps_combo.addItem(f"{f} fps", f)
        self.target_fps_combo.setCurrentIndex(max(0, [self.target_fps_combo.itemData(i)
            for i in range(self.target_fps_combo.count())].index(prof.target_fps)))
        fancy(self.target_fps_combo)

        self.target_color_combo = _DownComboBox()
        for k, label in (("keep", "不修改，分组保存"), ("sdr", "SDR"),
                         ("hlg", "HLG"), ("hdr10", "HDR10")):
            self.target_color_combo.addItem(label, k)
        self.target_color_combo.setCurrentIndex(max(0, [self.target_color_combo.itemData(i)
            for i in range(self.target_color_combo.count())].index(prof.target_color)))
        fancy(self.target_color_combo)

        # 所有可外露 combo 的元数据(单一来源): key -> (显示名, 颜色, combo)
        self._combo_defs = {
            "codec":         ("视频编码", T.DANGER, self.codec_combo),
            "audio_bitrate": ("音频", T.DANGER, self.audio_combo),
            "keyint_sec":    ("关键帧间隔", T.WARN, self.keyint_combo),
            "crf":           ("码率质量", T.TEXT_SUB, self.crf_combo),
            "target_res":    ("分辨率", T.WARN, self.target_res_combo),
            "target_fps":    ("帧率", T.WARN, self.target_fps_combo),
            "target_color":  ("HDR", T.WARN, self.target_color_combo),
        }
        for name, color, cb in self._combo_defs.values():
            cb.setObjectName("normCombo")
            cb.style().unpolish(cb)
            cb.style().polish(cb)

        # v242: ⚙ 按钮打开参数配置弹窗(固定在参数条末尾, 不参与 combo 压缩)
        self.gear_btn = QPushButton()
        self.gear_btn.setObjectName("normGearBtn")
        # v242: ⚙(U+2699) 在微软雅黑没有字形会渲染成豆腐块 —— 用项目现成的自绘图标
        self.gear_btn.setIcon(make_icon("gear", T.TEXT_SUB, 16))
        self.gear_btn.setToolTip("参数配置：查看全部参数、设置目标值／默认／外露")
        self.gear_btn.setFixedSize(30, 26)
        self.gear_btn.setCursor(Qt.PointingHandCursor)
        self.gear_btn.setStyleSheet(
            f"QPushButton#normGearBtn{{background:{T.SURFACE_ALT};border:1px solid {T.BORDER};"
            f"color:{T.TEXT_SUB};font-size:16px;border-radius:5px;}}"
            f"QPushButton#normGearBtn:hover{{background:{T.HOVER_SOFT};border-color:{T.ACCENT_BORDER};}}")
        self.gear_btn.clicked.connect(self.paramsRequested.emit)

        def rebuild_param_bar():
            # 先把所有 combo + 齿轮从旧格子里摘出来, 避免随旧 widget 一起被删
            for _n, _c, _cb in self._combo_defs.values():
                _cb.setParent(None)
            self.gear_btn.setParent(None)
            while pbl.count():
                item = pbl.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            self._param_cells = []
            for key in settings.load_norm_exposed():
                defn = self._combo_defs.get(key)
                if defn is None:
                    continue
                name, color, cb = defn
                cell(name, color, cb, fit_combo_width(cb))
            pbl.addWidget(self.gear_btn)
            # v244: 旧格子 deleteLater 后新布局尚未激活, 此刻 sizeHint().height()
            # 会返回 0 —— 主窗口据此 setFixedHeight(0) 会把整条参数栏锁死成 0 高
            # (弹窗保存后顶栏空白, 重启才恢复)。这里强制重算一次。
            pbl.invalidate()
            pbl.activate()

        self._rebuild_param_bar = rebuild_param_bar
        rebuild_param_bar()

        # v234: 删除颜色图例("红=必须统一 · 橙=建议统一 · 灰=仅供参考") —— 参数栏
        # 已按红/橙/灰着色且各控件自带含义, 图例占一整行属冗余。
        # 「保存为默认」独占一行靠右, 给底部三按钮腾出一行。
        legend_row = QHBoxLayout()
        legend_row.addStretch(1)
        self.saved_tip = QLabel("")
        self.saved_tip.setStyleSheet(f"color: {T.OK}; font-size: 12px;")
        legend_row.addWidget(self.saved_tip)
        self.save_btn = QPushButton("保存为默认")
        self.save_btn.setToolTip("记住当前档位；只新增 normalize_profile 配置键，不动已有快捷键")
        self.save_btn.clicked.connect(self.saveRequested)
        legend_row.addWidget(self.save_btn)
        root.addLayout(legend_row)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.start_btn = QPushButton("开始统一")
        self.start_btn.setProperty("accent", True)
        self.start_btn.setMinimumWidth(88)
        self.start_btn.clicked.connect(self.startRequested)
        foot.addWidget(self.start_btn)
        self.reveal_btn = QPushButton("打开位置")
        self.reveal_btn.setToolTip("在资源管理器中显示统一生成的副本所在文件夹")
        self.reveal_btn.clicked.connect(self.revealRequested)
        foot.addWidget(self.reveal_btn)
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
        foot.addWidget(self.edit_btn)
        root.addLayout(foot)

        for combo in (self.codec_combo, self.crf_combo, self.keyint_combo,
                      self.audio_combo, self.target_res_combo,
                      self.target_fps_combo, self.target_color_combo):
            combo.currentIndexChanged.connect(self._on_profile_edited)
        self._profile_ready = True

    def _on_profile_edited(self, *_):
        """v217: 存档"当前档位"再通知刷新。

        生成工作副本用的是这个档, 解析副本也必须用同一个档 —— 否则用户没点
        「保存为默认」时, 转码完的副本永远解析不到, 合并/导出悄悄退回原片。
        """
        if not getattr(self, "_profile_ready", False):
            return
        from ..merge import settings as _st
        _st.save_normalize_profile_current(self.profile())
        self.profileChanged.emit()

    def param_host(self) -> QWidget:
        """交给顶栏 reparent 的参数栏(避免主窗口碰内部下拉)。"""
        return self.param_bar

    def param_cells(self) -> list:
        """(combo, fit 宽度) 列表: 给顶栏 NormParamSlot 做压缩分配用(v228)。"""
        return list(self._param_cells)

    def rebuild_params(self):
        """v242: 弹窗改完档位/外露后调用 —— 把 settings 的值同步回本 panel 的
        combo(顶栏外露的控件), 重建格子, 并让调用方重新 register 给槽。"""
        clean = settings._clean_normalize_profile(settings.load_normalize_profile_current())
        mapping = {
            "codec": clean["codec"], "audio_bitrate": clean["audio_bitrate"],
            "keyint_sec": clean["keyint_sec"], "crf": clean["crf"],
            "target_res": clean["target_res"], "target_fps": clean["target_fps"],
            "target_color": clean["target_color"],
        }
        self._profile_ready = False
        for key, val in mapping.items():
            defn = self._combo_defs.get(key)
            if defn is None:
                continue
            cb = defn[2]
            idx = [cb.itemData(i) for i in range(cb.count())]
            if val in idx and cb.currentIndex() != idx.index(val):
                cb.setCurrentIndex(idx.index(val))
        self._profile_ready = True
        self._rebuild_param_bar()

    def _row(self, name, color, widget):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        k = QLabel(name)
        k.setFixedWidth(96)
        k.setMinimumHeight(24)
        k.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        k.setStyleSheet(f"color: {color}; font-size: 12px;")
        h.addWidget(k)
        widget.setMinimumHeight(max(24, widget.minimumHeight()))
        h.addWidget(widget, 1)
        return row

    def profile(self) -> dict:
        # v242: 7 个 combo 始终是权威(含画面目标)。顶栏 combo 改动直接读 combo;
        # 弹窗改完 settings 后由 rebuild_params() 把值同步回 combo, 两条路都一致。
        # v260: 顶栏无音频编码下拉, 从当前档读取保留弹窗设的值(否则会被默认 aac 冲掉)
        _cur = settings.load_normalize_profile_current()
        return {
            "codec": self.codec_combo.currentData() or "main",
            "crf": int(self.crf_combo.currentData() or 20),
            "keyint_sec": int(self.keyint_combo.currentData() or 2),
            "audio_bitrate": int(self.audio_combo.currentData() or 192),
            "audio_codec": _cur.get("audio_codec") or "aac",
            "target_res": self.target_res_combo.currentData() or "keep",
            "target_fps": self.target_fps_combo.currentData() or "keep",
            "target_color": self.target_color_combo.currentData() or "keep",
        }

    def set_result(self, color: str, icon: str, text: str, enabled: bool):
        """刷新结论条(v254: 对比清单正上方的纯文字两行, 无底色/无图标)。

        v218: text 为空 = 没什么可说的(比如一个视频都还没加), 直接收起整条,
        不再顶一句"先把视频加进来"占着右栏。
        """
        if not text:
            self.banner.setVisible(False)
            self.start_btn.setEnabled(bool(enabled))
            return
        self.banner.setVisible(True)
        # v254: 纯文字, 状态色落在文字上(成功=主文字色, 失败=红), 不再有底色块和√图标。
        tc = T.DANGER if color == T.DANGER else T.TEXT
        self.banner_text.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {tc};")
        self.banner_text.setText(text)
        self.start_btn.setEnabled(bool(enabled))

    def set_saved(self, text: str):
        self.saved_tip.setText(text)

    def set_selection(self, photos: list, specs: dict, path_infos: dict | None = None):
        """v209: 刷新预览图 + 参数对比(与合并右栏同款)。"""
        photos = list(photos or [])
        specs = specs or {}
        self.summary.setText(compare_model.selection_summary(photos, specs))
        self.preview.set_photo(photos[0] if photos else None)
        sel_specs = [specs.get(getattr(p, "path", "")) for p in photos
                     if specs.get(getattr(p, "path", ""))]
        if not photos or not sel_specs:
            self.compare_body.setText(
                '<span style="color:%s;">在左侧勾选视频后，这里显示所选视频的规格对比。</span>'
                % T.TEXT_WEAK)
            return
        rows = compare_model.build_rows(sel_specs, compact=True)
        if not rows:
            self.compare_body.setText(
                '<span style="color:%s;">所选视频还没检测出规格，稍候再试。</span>'
                % T.TEXT_WEAK)
            return
        parts, prev = [], object()
        for i, row in enumerate(rows):
            if row["level"] != prev:
                if parts:
                    parts.append("")
                    # v251: 「建议一致」「仅供参考」档位上方加蓝色加粗横线; 首档不加
                    if row["level"] in ("warn", None):
                        parts.append(compare_model.SECTION_LINE_HTML)
                sc = (T.DANGER if row["level"] == "fatal"
                      else T.WARN if row["level"] == "warn" else T.TEXT_SUB)
                parts.append(
                    f'<span style="color:{sc}; font-size:13px; font-weight:600">'
                    f'{compare_model.section_title(row["level"])}</span>')
                prev = row["level"]
            elif compare_model.media_divider_before(rows, i):
                parts.append(compare_model.DIVIDER_HTML)
            uniq = []
            for v in row["values"]:
                if v not in uniq:
                    uniq.append(v)
            val_text = " / ".join(html.escape(str(v)) for v in uniq)
            parts.append(
                f'<span style="color:{row["color"]}">{html.escape(row["cn"])}</span>'
                f'{compare_model.info_html(row["key"])}'
                f'：<span style="color:{T.TEXT}">{val_text}</span>')
        self.compare_body.setText(compare_model.join_rows(parts))


class NormWorkspace(QObject):
    """统一形态协调者: 持有素材/规格/计划, 参数变化重算并刷新左右两块。"""

    startRequested = Signal(object, object)   # (profile dict, normalize.NormPlan)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preview = NormPreview()
        self.panel = NormPanel()
        self.photos: list = []
        self.specs: dict = {}
        self.ffmpeg = None
        self.ffprobe = None
        self.plan = normalize.NormPlan()
        self.selected_paths: set = set()   # v207: 网格勾选的视频(空=视为全选)
        self.panel.profileChanged.connect(self.reload)
        self.panel.saveRequested.connect(self._save_default)
        self.panel.startRequested.connect(self._emit_start)

    def set_context(self, photos, specs, ffmpeg, ffprobe):
        self.photos = list(photos or [])
        self.specs = dict(specs or {})
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.reload()

    def set_specs(self, specs):
        """探测回调补规格后调用, 保留当前参数档原位重算。"""
        self.specs = dict(specs or {})
        self.reload()

    def profile(self) -> dict:
        return self.panel.profile()

    @staticmethod
    def _codec_label(codec) -> str:
        c = str(codec or "").lower()
        return {"h264": "H.264", "avc1": "H.264", "hevc": "H.265",
                "h265": "H.265"}.get(c, (c.upper() or "未知编码"))

    @staticmethod
    def _fps_label(rfr) -> str:
        """帧率显示名: '30000/1001' → '29.97fps', '30/1' → '30fps'。

        ⚠️ v220: 以前是 `str(rfr).replace("/1", "")` —— '30000/1001' 会被啃成
        '30000001fps'(用户看到的"帧率显示错误"就是它), 而且这个串还是分组键,
        两档真实帧率反而被拼进同一组/拆成两组都说不准。
        """
        s = str(rfr or "").strip()
        try:
            if "/" in s:
                a, b = s.split("/", 1)
                num, den = float(a), float(b)
                v = (num / den) if den else 0.0
            else:
                v = float(s)
        except Exception:
            return f"{s}fps" if s and s not in ("0/0", "0") else "帧率未知"
        if v <= 0:
            return "帧率未知"
        near = round(v)
        if abs(v - near) < 0.01:          # 整数帧率不带小数点
            return f"{int(near)}fps"
        return f"{v:.2f}".rstrip("0").rstrip(".") + "fps"

    def groups_for_grid(self) -> list:
        """把 NormPlan 铺成三级网格: 编码 → 分辨率 → 帧率。

        给每个 Photo 写 norm_key=(codec, 分辨率, 帧率)、norm_labels=(三级显示名)、
        norm_need(是否需转码 0/1), 供 PhotoModel._rebuild_norm 按前缀逐级折叠。
        返回一个简单分组列表(仅用于"有没有内容"判断 + 兼容旧调用)。
        """
        by_path = {getattr(p, "path", ""): p for p in self.photos}
        item_by_path = {getattr(it, "path", ""): it
                        for it in (self.plan.items or [])}
        spec_by_path = {p: getattr(it, "spec", None) for p, it in item_by_path.items()}
        groups: dict[tuple, list] = {}
        for path, photo in by_path.items():
            it = item_by_path.get(path)
            spec = (getattr(it, "spec", None) if it else None) or self.specs.get(path) or {}
            codec = self._codec_label(spec.get("video_codec"))
            w, h = spec.get("width"), spec.get("height")
            res = f"{w}×{h}" if w and h else "分辨率未知"
            # v244: 横竖屏提示(只给分辨率级标题, 画在"N个"右侧)。方形/未知不提示。
            photo.norm_orientation = ("横屏" if w > h else "竖屏") if w and h and w != h else ""
            fps = self._fps_label(spec.get("r_frame_rate"))
            # v219: HDR 是转码消灭不了的差异(build_cmd 原样复制 color_transfer),
            # 只能在分辨率这一级按 HDR 类别分开成组 —— 层级仍是三级, 不加开关。
            hdr = normalize.hdr_kind(spec)
            res_key = f"{res}|{hdr}"
            res_lab = res if hdr == "SDR" else f"{res} · {hdr}"
            photo.norm_key = (codec, res_key, fps)
            photo.norm_labels = (codec, res_lab, fps)
            photo.norm_need = 1 if (it is not None and getattr(it, "need", False)) else 0
            groups.setdefault(photo.norm_key, []).append(photo)
        return [{"key": k, "photos": v} for k, v in groups.items()]

    def plan_for_paths(self, paths) -> "normalize.NormPlan":
        """按勾选路径裁剪 plan, 只统一勾选到的视频(items/groups/计数都重算)。"""
        keep = set(paths or [])
        if not keep:
            return normalize.NormPlan()
        picked = [it for it in (self.plan.items or [])
                  if getattr(it, "path", "") in keep]
        out = normalize.NormPlan()
        # v219: 子集的目标声道/need 要重算 —— 全批里那个多声道素材可能没被勾上,
        # 沿用全批结论会把"单声道也要转立体声"的结论带进来。
        out.target_channels = normalize.pick_target_channels(
            [getattr(it, "spec", None) for it in picked])
        prof = normalize.NormProfile.from_dict(self.panel.profile())
        kept_items = []
        for src in picked:
            it = copy.copy(src)          # 不污染主 plan 的 need/reasons
            if not getattr(src, "error", ""):
                fix, block = normalize._diff_reasons(
                    getattr(src, "spec", None) or {}, prof, out.target_channels)
                it.need = bool(fix)
                it.reasons = fix + block
                it.group_key = normalize._group_key(
                    normalize.target_spec(getattr(src, "spec", None) or {},
                                          prof, out.target_channels), prof)
            kept_items.append(it)
            out.total_duration += it.duration
            if it.need:
                out.need_count += 1
                out.need_duration += it.duration
            if it.error:
                out.errors.append(it.error)
        out.items = kept_items
        grouped: dict = {}
        for it in kept_items:
            grouped.setdefault(it.group_key, []).append(it)
        for key, members in grouped.items():
            head = normalize.target_spec(members[0].spec or {}, prof,
                                         out.target_channels)
            out.groups.append({
                "key": key,
                "label": normalize._group_label(head, len(members)),
                "items": members,
                "need_count": sum(1 for m in members if m.need),
            })
        return out

    def reload(self):
        paths = [getattr(p, "path", "") for p in self.photos
                 if getattr(p, "path", "")]
        prof = normalize.NormProfile.from_dict(self.panel.profile())
        try:
            self.plan = normalize.plan(paths, self.ffprobe, prof, self.specs)
        except Exception as e:
            self.plan = normalize.NormPlan()
            self.plan.errors = [f"分析失败：{e}"]
        self._refresh_result()
        self.preview.set_plan(self.plan)
        # v209: 右侧预览/参数对比跟随当前勾选(空=全部)
        keep = set(self.selected_paths or [])
        sel = [p for p in self.photos if not keep or getattr(p, "path", "") in keep]
        specs = {}
        for it in (self.plan.items or []):
            if getattr(it, "spec", None):
                specs[getattr(it, "path", "")] = it.spec
        specs.update(self.specs)
        self.panel.set_selection(sel, specs)

    def _refresh_result(self):
        plan = self.plan
        if not plan.items:
            # v218: 一个视频都没有 → 什么都不提示(主区已经是"先添加文件"的空态),
            # 横幅整条收起。⚠️ 这条必须排在最前: 以前先判 ffprobe, 于是"一个视频都
            # 没加"时反而顶着"未找到 ffprobe / 先把视频加进来", 平白吓用户一跳。
            color, icon, text = T.DANGER, "×", ""
        elif not self.ffprobe:
            color, icon = T.DANGER, "×"
            text = "未找到 ffprobe，无法分析。请先在「合并」页指定 FFmpeg 路径。"
        elif plan.errors:
            color, icon = T.DANGER, "×"
            text = (f"{len(plan.errors)} 个文件读取失败，不能开始统一："
                    + "；".join(plan.errors[:2]))
        else:
            color, icon = T.OK, "√"
            # v254: 精简为两行 —— "N 个视频 → M 个合并组" / "K 个需转码，总计 X 分钟";
            # 原来分行处的分号不再显示, 直接换行。用渲染可靠的 "→" 而非 emoji。
            if plan.need_count:
                text = (f"{len(plan.items)} 个视频 → {len(plan.groups)} 个合并组\n"
                        f"{plan.need_count} 个需转码，总计 {plan.total_duration / 60:.1f} 分钟")
            else:
                text = (f"{len(plan.items)} 个视频 → {len(plan.groups)} 个合并组\n"
                        "无需转码，可直接快合")
        self.panel.set_result(color, icon, text,
                              not plan.errors and bool(plan.items) and bool(self.ffprobe))

    def _save_default(self):
        ok = settings.save_normalize_profile(self.panel.profile())
        if ok:
            # 默认档也是"当前档": 副本解析要跟着它, 否则保存完立刻失效
            settings.save_normalize_profile_current(self.panel.profile())
        self.panel.set_saved("已保存为默认" if ok else "保存失败")
        if ok:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2500, lambda: self.panel.set_saved(""))

    def _emit_start(self):
        if self.plan.errors or not self.plan.items or not self.ffprobe:
            return
        plan = self.plan
        if self.selected_paths:
            plan = self.plan_for_paths(self.selected_paths)
            if not plan.items:
                return
        self.startRequested.emit(self.panel.profile(), plan)
