# -*- coding: utf-8 -*-
"""剪辑页工具箱的四个弹窗: 改帧率(变速) / 大幅削减 / 检测 / 修复时长。

设计原则(用户定的):
- 一句话先说"会不会坏" —— 无损就写无损, 有损就不许藏;
- 帧率与倍速是同一件事, 改一个另一个立刻跟着变(不是两个独立设置);
- 结果先算给用户看(时长会变成多少、还剩几帧), 别让人点完才知道。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QDialog,
                               QDoubleSpinBox, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QProgressBar, QPushButton,
                               QScrollArea, QSpinBox, QTabWidget, QTextEdit,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from . import theme as T
from .widgets import CheckCheckBox


def _open_default(path: str):
    """用系统默认程序打开(视频=默认播放器, 图片=看图)。"""
    try:
        os.startfile(path)      # noqa: S606 (Windows)
    except Exception:
        pass


def _fmt(t: float) -> str:
    t = max(0.0, float(t or 0.0))
    if t >= 3600:
        h = int(t // 3600)
        return f"{h} 小时 {int((t - h * 3600) // 60)} 分"
    if t >= 60:
        m = int(t // 60)
        return f"{m} 分 {t - m * 60:.1f} 秒"
    return f"{t:.1f} 秒"


def _fmt_size(n: int) -> str:
    """文件大小(1.23 MB（1,234,567 字节）)。"""
    try:
        n = int(n or 0)
    except Exception:
        n = 0
    if n <= 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    v, i = float(n), 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.2f} {units[i]}（{n:,} 字节）"


def _rgba(color: str, alpha: float) -> str:
    from PySide6.QtGui import QColor
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.2f})"


def _spin_w(widget, sample: str, extra: int = 32) -> int:
    """数字输入框该多宽: 按"最长会出现的那个数"量出来, 别一律 104(v155)。

    ⚠️ 用 fontMetrics 量, 不写死像素: 换字号/换缩放比之后写死的宽度不是太窄
    (数字被切) 就是太宽(用户嫌空框)。
    """
    try:
        w = widget.fontMetrics().horizontalAdvance(str(sample))
    except Exception:
        w = 8 * len(str(sample))
    return max(56, int(w) + extra)


class _Card(QFrame):
    """浅色说明块(不抢眼, 但有边界)。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame#toolCard {{ background: {T.SURFACE_ALT};"
            f" border: 1px solid {T.BORDER}; border-radius: 8px; }}")
        self.setObjectName("toolCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(4)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;"
                                 " background: transparent; border: none;")
        lay.addWidget(self.label)

    def setText(self, text: str):
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()


class _ModalBase(QDialog):
    def __init__(self, parent, title: str, width: int = 560):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(width)
        self.setModal(True)
        self._centered = False
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(18, 16, 18, 14)
        self.root.setSpacing(10)

    def showEvent(self, event):
        """第一次显示时居中(v136)。

        ⚠️ 原来这些弹窗一开就贴在**屏幕左上角**: 构造时尺寸还没算完, Qt 没法定中;
        而 parent 又常常是"在后面没关的主窗口", 就更没参照物了。
        所以在 showEvent 里按**已经算好的实际尺寸**摆一次, 只摆第一次
        (之后用户自己挪过位置, 不能每次显示都拽回来)。
        """
        super().showEvent(event)
        if self._centered:
            return
        self._centered = True
        ref = None
        p = self.parentWidget()
        if p is not None:
            ref = p.window()
        if ref is None or not ref.isVisible():
            scr = self.screen()
            area = scr.availableGeometry() if scr is not None else None
        else:
            area = ref.frameGeometry()
        if area is None:
            return
        g = self.frameGeometry()
        g.moveCenter(area.center())
        self.move(g.topLeft())

    def _buttons(self, ok_text: str, ok_slot, cancel: bool = True):
        row = QHBoxLayout()
        row.addStretch(1)
        if cancel:
            btn = QPushButton("取消")
            btn.clicked.connect(self.reject)
            row.addWidget(btn)
        ok = QPushButton(ok_text)
        ok.setProperty("accent", True)
        ok.setDefault(True)
        ok.clicked.connect(ok_slot)
        row.addWidget(ok)
        self.root.addLayout(row)
        self.ok_btn = ok


class FpsDialog(_ModalBase):
    """帧率 ↔ 播放速度 一起改(改一个, 另一个立刻跟着变)。

    v185: 不再另存新素材文件 —— 改动只记在选中的那一段上, 导出时才落地。
    弹窗里显示的帧率是「等效帧率」(源帧率 × 倍速), 符合「改帧率就是变速」。
    """

    def __init__(self, parent, name: str, orig_fps: float, duration: float,
                 out_dir: str = "", cur_speed: float = 1.0):
        super().__init__(parent, "改帧率 / 变速", 580)
        self.name = name
        self.orig_fps = float(orig_fps or 0.0)
        self.duration = float(duration or 0.0)
        self.out_dir = out_dir
        self.cur_speed = float(cur_speed or 1.0) or 1.0
        self._sync = False
        self._build()
        self._refresh()

    def _build(self):
        head = QLabel(f"<b>{self.name}</b>")
        head.setTextFormat(Qt.RichText)
        self.root.addWidget(head)

        card = _Card(
            "只改时间线上选中的这一段，素材文件不动、其它片段也不受影响。\n"
            "改动随时能改、Ctrl+Z 能退；到导出那一刻才真正生效。")
        self.root.addWidget(card)

        row = QHBoxLayout()
        row.setSpacing(8)
        lab1 = QLabel("新帧率")
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setDecimals(2)
        self.fps_spin.setRange(1.0, 480.0)
        self.fps_spin.setSingleStep(1.0)
        # ⚠️ 单位不做 suffix: suffix 是输入框里的文字, 全选/双击就把 "fps" 一起选中了。
        #    单位单独摆在外面(v146 用户要求)。
        # v155: 宽度按"最长会出现的数"算(480.00 / 20.000), 别留一大截空框。
        self.fps_spin.setFixedWidth(_spin_w(self.fps_spin, "480.00"))
        self.fps_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        fps_unit = QLabel("fps")
        fps_unit.setStyleSheet(f"color: {T.TEXT_WEAK}; background: transparent;"
                               " border: none;")
        lab2 = QLabel("播放速度")
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setDecimals(3)
        self.speed_spin.setRange(0.05, 20.0)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setFixedWidth(_spin_w(self.speed_spin, "20.000"))
        self.speed_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        speed_unit = QLabel("×")
        speed_unit.setStyleSheet(f"color: {T.TEXT_WEAK}; background: transparent;"
                                 " border: none;")
        row.addWidget(lab1)
        row.addWidget(self.fps_spin)
        row.addWidget(fps_unit)
        row.addSpacing(14)
        row.addWidget(lab2)
        row.addWidget(self.speed_spin)
        row.addWidget(speed_unit)
        row.addStretch(1)
        self.root.addLayout(row)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(QLabel("常用："))
        for mul in (0.5, 1.0, 2.0, 4.0, 8.0):
            b = QPushButton(f"{mul:g}×")
            b.setFixedHeight(27)
            b.setMinimumWidth(60)
            b.setStyleSheet("padding: 0 6px;")
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, m=mul: self._set_speed(m))
            chips.addWidget(b)
        chips.addStretch(1)
        self.root.addLayout(chips)

        self.hint = _Card("")
        self.root.addWidget(self.hint)

        self.root.addWidget(QLabel(
            f"<span style='color:{T.TEXT_WEAK}'>导出时只有这一段单独变速，其余片段仍然无损拼接。</span>"))

        self.fps_spin.valueChanged.connect(self._on_fps)
        self.speed_spin.valueChanged.connect(self._on_speed)
        self.fps_spin.setValue((self.orig_fps or 25.0) * self.cur_speed)
        self._buttons("确定", self.accept, cancel=False)

    # ------------------------------------------------------------ 联动
    def _on_fps(self, v: float):
        if self._sync or self.orig_fps <= 0:
            return
        self._sync = True
        self.speed_spin.setValue(max(0.05, min(20.0, v / self.orig_fps)))
        self._sync = False
        self._refresh()

    def _on_speed(self, v: float):
        if self._sync or self.orig_fps <= 0:
            return
        self._sync = True
        self.fps_spin.setValue(max(1.0, min(480.0, v * self.orig_fps)))
        self._sync = False
        self._refresh()

    def _set_speed(self, mul: float):
        self.speed_spin.setValue(mul)

    def _refresh(self):
        new_fps = self.fps_spin.value()
        speed = self.speed_spin.value()
        after = (self.duration / speed) if speed > 0 else 0.0
        self.hint.setText(
            f"<b>{self.orig_fps:g} fps → {new_fps:g} fps</b>"
            f"　（{speed:g} 倍速）<br>"
            f"时长：{_fmt(self.duration)} → <b>{_fmt(after)}</b>")
        # 没改动就别让人点（跟"打开弹窗时的倍速"比, 不是跟 1.0 比）
        ok_btn = getattr(self, "ok_btn", None)
        if ok_btn is not None:
            ok_btn.setEnabled(abs(speed - self.cur_speed) > 1e-4)

    def value(self) -> float:
        return float(self.fps_spin.value())


class VolumeDialog(_ModalBase):
    """片段音量(响度, v169): 只把倍数记在片段上, 导出时才对这段声音生效。

    不生成新文件、随时可改可撤销 —— 代价是导出时这一段声音要重新编码
    (音量滤镜做不了流复制), 画面不受影响。0% = 这段不要声音(和「静音」等效)。
    """

    def __init__(self, parent, name: str, cur: float, many: int = 1):
        title = "音量" if many <= 1 else f"音量（{many} 段）"
        super().__init__(parent, title, 480)
        self.orig = max(0.0, float(cur or 1.0))

        head = QLabel(f"<b>{name}</b>")
        head.setTextFormat(Qt.RichText)
        self.root.addWidget(head)

        self.root.addWidget(_Card(
            "改这一段声音的响度。0% = 没声音（和「静音」一个效果）。\n"
            "代价：导出时这一段声音要重新编码，画面不受影响。"))

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("音量"))
        self.spin = QSpinBox()
        self.spin.setRange(0, 200)
        self.spin.setSingleStep(5)
        self.spin.setSuffix("%")
        self.spin.setFixedWidth(100)
        row.addWidget(self.spin)
        row.addStretch(1)
        self.root.addLayout(row)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(QLabel("常用："))
        for pct in (0, 50, 100, 150, 200):
            b = QPushButton(f"{pct}%")
            b.setFixedHeight(27)
            b.setMinimumWidth(56)
            b.setStyleSheet("padding: 0 6px;")
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=pct: self.spin.setValue(v))
            chips.addWidget(b)
        chips.addStretch(1)
        self.root.addLayout(chips)

        self.hint = _Card("")
        self.root.addWidget(self.hint)
        self.spin.valueChanged.connect(self._refresh)
        self.spin.setValue(int(round(self.orig * 100)))
        # v170: 不要「取消」按钮 —— 只有一个「应用」, 点右上角 × 就是不改
        self._buttons("应用", self.accept, cancel=False)
        self._refresh()

    def _refresh(self):
        v = self.spin.value()
        # v170: 显示"响度"(dB)换算, 比 "100% = 不变" 有用得多:
        #   音量是**幅度**倍数, 转 dB 就是 20·log10(倍数); 50% ≈ -6 dB,
        #   200% ≈ +6 dB, 0% 是静音(对数上是负无穷)。
        if v <= 0:
            text = "响度：<b>静音</b>"
        else:
            from math import log10
            db = 20.0 * log10(v / 100.0)
            if abs(db) < 0.05:
                text = "响度：<b>0 dB</b>（原样）"
            else:
                text = f"响度：<b>{db:+.1f} dB</b>"
        self.hint.setText(text)
        ok_btn = getattr(self, "ok_btn", None)
        if ok_btn is not None:
            ok_btn.setEnabled(abs(v / 100.0 - self.orig) > 1e-4)

    def value(self) -> float:
        """音量倍数(1.0 = 原样)。"""
        return self.spin.value() / 100.0


# ---------------------------------------------------------------- 属性(v172)
# 「全部元数据」里字段名按来源分组上色: 一眼看出这条是相机写的、还是容器写的。
# 只上"字段名"这一列, 值统一深色 —— 满屏彩字反而看不清内容。
_GROUP_COLORS = {
    "EXIF": "#2F6FD0",         # 蓝
    "XMP": "#7A4FD0",          # 紫
    "IPTC": "#0E7C7B",         # 青
    "QuickTime": "#B8611C",    # 棕橙
    "Track": "#8A6D3B",
    "Media": "#8A6D3B",
    "Keys": "#8A6D3B",
    "ItemList": "#8A6D3B",
    "UserData": "#8A6D3B",
    "GPS": "#2E7D32",          # 绿
    "Composite": "#B03A6E",    # 品红
    "ICC_Profile": "#6B5B95",
    "PNG": "#5A7D2A",
    "JFIF": "#5A7D2A",
    "File": "#6B7280",         # 灰(和系统信息一档, 不抢眼)
    "System": "#6B7280",
    "ExifTool": "#6B7280",
}
_GROUP_CN = {"File": "文件", "Composite": "合成", "System": "系统",
             "ExifTool": "ExifTool 工具", "Track": "轨道", "Media": "媒体",
             "Other": "其它"}
_GROUP_ORDER = ["EXIF", "XMP", "IPTC", "QuickTime", "Track", "Media", "GPS",
                "Keys", "ItemList", "UserData", "ICC_Profile", "PNG", "JFIF",
                "File", "Composite", "System", "ExifTool"]


def _fmt_stamp(ts: float) -> str:
    from datetime import datetime
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def _page(sections) -> QWidget:
    """[("分区名", [(字段, 值), …]), …] → 一页可滚动的只读表格。"""
    from ..core import fields as F      # v198: 显示层统一去掉时区后缀
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    first = True
    for title, rows in sections:
        if not rows:
            continue
        if not first:
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"color: {T.BORDER}; background: {T.BORDER};")
            lay.addWidget(sep)
        first = False
        cap = QLabel(title)
        cap.setStyleSheet(f"color: {T.TEXT}; font-size: 13px; font-weight: 600;")
        lay.addWidget(cap)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnMinimumWidth(0, 104)
        grid.setColumnStretch(1, 1)
        for r, (key, val) in enumerate(rows):
            k = QLabel(key)
            k.setStyleSheet(f"color: {T.TEXT_SUB}; font-size: 12px;")
            k.setAlignment(Qt.AlignRight | Qt.AlignTop)
            v = QLabel(F.display_value(val))
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setStyleSheet(f"color: {T.TEXT}; font-size: 12px;")
            grid.addWidget(k, r, 0)
            grid.addWidget(v, r, 1)
        lay.addLayout(grid)
    lay.addStretch(1)
    return page


def _scroll(page: QWidget) -> QWidget:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setWidget(page)
    return sa


class _MetaWorker(QThread):
    """后台读一个文件的完整元数据(EXIF/XMP/QuickTime… 一个 exiftool 进程)。

    为什么不在构造函数里读: 几百个 tag 要起进程, 主线程直接读会卡半秒,
    弹窗就"点了没反应"。读回来再填表格。
    """
    done = Signal(str, object)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        rec: dict = {}
        et = None
        try:
            from ..core.exiftool import ExifTool
            et = ExifTool()
            et.start()
            rows = et.read_json([self.path])
            if rows:
                rec = dict(rows[0])
                rec.pop("SourceFile", None)
        except Exception:
            rec = {}
        finally:
            try:
                if et is not None:
                    et.close()
            except Exception:
                pass
        self.done.emit(self.path, rec)


class ClipPropsDialog(_ModalBase):
    """属性(v172): 三页, 和「合并」页的属性窗口同一个口径。

    ① 常规      —— 文件本身(路径/大小/时间) + 时间线上这一段的参数(有片段才有)
    ② 视频信息  —— 源文件的分辨率/帧率/编码/音轨(图片叫「拍摄信息」, 音频叫「音频信息」)
    ③ 全部元数据 —— 源文件 EXIF/XMP/QuickTime 全量表格, 字段名按来源分组上色

    左侧素材右键(只传 path, 没有片段)和时间线/右栏片段右键(传 clip)共用这一个窗,
    所以"素材还没放进时间线也能看属性"。关窗只靠右上角 ×, 底下不再放按钮。
    """

    KIND_WORD = {"video": "视频", "audio": "音频", "image": "图片"}
    DETAIL_TITLE = {"video": "视频信息", "audio": "音频信息", "image": "拍摄信息"}
    # (分区, [(字段名, 取值 key)]); key 以 meta: 开头读 EXIF, 其余走 ffprobe 规格
    DETAIL_ROWS = {
        "video": [
            ("视频", [("时长", "duration"), ("分辨率", "width"),
                     ("帧率", "r_frame_rate"), ("编码", "video_codec"),
                     ("像素格式", "pix_fmt"), ("位深", "bits"),
                     ("编码档次", "profile"), ("编码等级", "level"),
                     ("HDR 传输", "color_transfer"), ("扫描方式", "field_order"),
                     ("色彩空间", "color_space"), ("色彩范围", "color_range"),
                     ("旋转", "rotation"), ("视频码率", "video_bitrate")]),
            ("音频", [("音频编码", "audio_codec"), ("音频档次", "audio_profile"),
                     ("采样格式", "sample_fmt"), ("采样率", "sample_rate"),
                     ("声道数", "channels"), ("声道布局", "channel_layout")]),
            ("其它", [("容器格式", "format_name"), ("流结构", "nb_streams"),
                     ("拍摄时间", "meta:taken"), ("设备", "meta:device"),
                     ("GPS", "meta:gps")]),
        ],
        "audio": [
            ("音频", [("时长", "duration"), ("音频编码", "audio_codec"),
                     ("采样格式", "sample_fmt"), ("采样率", "sample_rate"),
                     ("声道数", "channels"), ("声道布局", "channel_layout")]),
            ("其它", [("容器格式", "format_name"), ("流结构", "nb_streams"),
                     ("拍摄时间", "meta:taken"), ("设备", "meta:device")]),
        ],
        "image": [
            ("拍摄", [("拍摄时间", "meta:taken"), ("相机", "meta:camera"),
                     ("镜头", "meta:lens"), ("曝光", "meta:exposure"),
                     ("测光 / 闪光", "meta:meter"), ("GPS", "meta:gps")]),
            ("画面", [("分辨率", "meta:size"), ("像素", "meta:mp"),
                     ("色彩 / 位深", "meta:color"), ("方向", "meta:orientation"),
                     ("处理软件", "meta:software")]),
        ],
    }

    def __init__(self, parent, clip=None, path: str = "",
                 track_label: str = "", fps: float = 0.0):
        from ..core.clips import kind_of
        self.clip = clip
        self.path = path or (getattr(clip, "source", "") if clip is not None else "")
        kind = getattr(clip, "kind", "") if clip is not None else ""
        self.kind = kind or kind_of(self.path)
        self.fps = float(fps or 0.0)
        self.meta: dict = {}
        self.spec: dict = {}
        self._probe_done = False       # ffprobe 跑完了(哪怕没结果) —— 别一直卡在"读取中"
        self._track_label = track_label
        self._meta_worker = None
        self._probe_worker = None
        self._value_labels: dict[str, QLabel] = {}

        super().__init__(parent, "属性", 680)
        # v174: 不锁死主界面 —— 合并页那个属性窗口就是非模态可多开的
        self.setWindowModality(Qt.NonModal)

        self.root.addWidget(self._header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_general(), "常规")
        self.tabs.addTab(self._tab_detail(),
                         self.DETAIL_TITLE.get(self.kind, "详细信息"))
        self.tabs.addTab(self._tab_raw(), "全部元数据")
        self.root.addWidget(self.tabs, 1)
        self.resize(700, 580)
        self._start_workers()

    # ---------------------------------------------------------------- 头部
    def _header(self) -> QWidget:
        """和「合并」属性窗口同款: 左上缩略图(点了用默认播放器放) + 文件名。"""
        from .props_dialog import _round_pixmap
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        name = ((getattr(self.clip, "name", "") if self.clip is not None else "")
                or os.path.basename(self.path) or "—")
        self.setWindowTitle(f"属性 — {name}")

        thumb = QLabel()
        thumb.setFixedSize(76, 76)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet(f"background: {T.THUMB_BG}; border-radius: 10px;"
                            f" color: {T.TEXT_WEAK}; font-size: 11px;")
        pix = QPixmap()
        try:
            from ..core.thumbs import thumb_pixmap
            pix = thumb_pixmap(self.path)
        except Exception:
            pix = QPixmap()
        if pix.isNull():
            thumb.setText("无预览")
        else:
            thumb.setPixmap(_round_pixmap(pix, 72))
        if self.path:
            thumb.setCursor(Qt.PointingHandCursor)
            thumb.setToolTip("用默认程序打开（视频=默认播放器）")
            thumb.mouseReleaseEvent = lambda ev, p=self.path: _open_default(p)
        lay.addWidget(thumb, 0, Qt.AlignVCenter)

        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)
        lab = QLabel(name)
        lab.setStyleSheet(f"color: {T.TEXT}; font-size: 15px; font-weight: 600;")
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(lab)
        info.addStretch(1)
        lay.addLayout(info, 1)
        return box

    # ---------------------------------------------------------------- 常规
    def _tab_general(self) -> QWidget:
        try:
            st = os.stat(self.path)
        except OSError:
            st = None
        rows = [("完整路径", self.path or "—"),
                ("大小", _fmt_size(st.st_size if st else 0)),
                ("类型", self._type_text())]
        if st is not None:
            rows.append(("修改时间", _fmt_stamp(st.st_mtime)))
            rows.append(("创建时间", _fmt_stamp(st.st_ctime)))
        sections = [("文件", rows)]
        if self.clip is not None:
            sections.append(("时间线上这一段", self._clip_rows()))
        return _scroll(_page(sections))

    def _type_text(self) -> str:
        word = self.KIND_WORD.get(self.kind, "")
        ext = os.path.splitext(self.path)[1].lstrip(".").upper()
        if word and ext:
            return f"{word}（{ext}）"
        return word or ext or "—"

    def _clip_rows(self):
        clip = self.clip
        kind = getattr(clip, "kind", "")
        speed = float(getattr(clip, "speed", 1.0) or 1.0)
        start = float(getattr(clip, "start", 0.0) or 0.0)
        dur = float(getattr(clip, "duration", 0.0) or 0.0)
        rows = [("所在轨道", self._track_label
                 or f"轨 {int(getattr(clip, 'track', 0)) + 1}")]
        if kind in ("video", "audio"):
            src_dur = float(getattr(clip, "src_duration", 0.0) or 0.0)
            rows.append(("源文件时长", _fmt(src_dur) if src_dur > 0 else "还没探测到"))
            rows.append(("用了源的哪一截",
                         f"{float(getattr(clip, 'src_in', 0.0)):.2f} 秒  →  "
                         f"{float(getattr(clip, 'src_out', 0.0)):.2f} 秒"))
        rows.append(("这一段多长", _fmt(dur)))
        rows.append(("在时间线上的位置", f"{start:.2f} 秒  →  {start + dur:.2f} 秒"))
        if kind == "video":
            rows.append(("帧率", f"{self.fps:g} fps" if self.fps > 0 else "还没探测到"))
            kf = len(getattr(clip, "keyframes", None) or [])
            rows.append(("可无损下刀的位置", f"{kf} 个关键帧" if kf else "还没探测到"))
        if kind in ("video", "audio"):
            rows.append(("倍速", f"{speed:g}×"))
            rows.append(("音量", self._volume_text(clip)))
            rows.append(("声音状态", self._sound_text(clip)))
        return rows

    # ---------------------------------------------------------------- 详细
    # 两列并排的分区(和「合并 → 视频信息」同一个排法): 左右各一块, 横向空间用满
    DETAIL_PAIR = {
        "video": ("视频", "音频"),
        "image": ("拍摄", "画面"),
        "audio": ("音频", "其它"),
    }

    def _tab_detail(self) -> QWidget:
        items = self.DETAIL_ROWS.get(self.kind)
        if not items:
            return _scroll(_page([("提示", [("", "这个文件没有可显示的详细信息")])]))
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)
        pair = self.DETAIL_PAIR.get(self.kind, ())
        rest = []
        top = QHBoxLayout()
        top.setSpacing(26)
        for title, rows in items:
            box = self._section_grid(title, rows)
            if title in pair:
                top.addWidget(box, 1)
            else:
                rest.append(box)
        if top.count():
            outer.addLayout(top)
        for box in rest:
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"color: {T.BORDER}; background: {T.BORDER};")
            outer.addWidget(sep)
            outer.addWidget(box)
        outer.addStretch(1)
        return _scroll(page)

    def _section_grid(self, title: str, rows) -> QWidget:
        """一个分区: 标题 + 「参数名 | 值」两列小表(值可被 ffprobe 异步回填)。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        cap = QLabel(title)
        cap.setStyleSheet(f"color: {T.TEXT}; font-size: 13px; font-weight: 600;")
        lay.addWidget(cap)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(5)
        grid.setColumnMinimumWidth(0, 104)
        grid.setColumnStretch(1, 1)
        for r, (label, key) in enumerate(rows):
            k = QLabel(label)
            # 参数名按致命度标色(和「合并 → 视频信息」同一套: 红=必须一致,
            # 橙=建议一致, 灰=仅供参考), 值统一深色
            k.setStyleSheet(
                f"color: {self._label_color(key)}; font-size: 12px;")
            k.setAlignment(Qt.AlignRight | Qt.AlignTop)
            v = QLabel(self._detail_value(key))
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
            grid.addWidget(k, r, 0)
            grid.addWidget(v, r, 1)
            self._value_labels[key] = v
        lay.addLayout(grid)
        lay.addStretch(1)
        return box

    @staticmethod
    def _label_color(key: str) -> str:
        if key.startswith("meta:"):
            return T.TEXT_SUB
        from ..merge import grouping
        for k, _cn, lv in grouping.FIELD_LEVELS:
            if k == key:
                if lv == "fatal":
                    return T.DANGER
                if lv == "warn":
                    return T.WARN
                break
        return T.TEXT_SUB

    def _detail_value(self, key: str) -> str:
        if key.startswith("meta:"):
            return self._meta_field(key[5:])
        if self.kind in ("video", "audio") and not self.spec:
            # ffprobe 没装/没跑通 → 别永远显示"读取中", 直接给横线
            return "—" if self._probe_done else "读取中…"
        from ..merge import grouping
        val = grouping.field_value(self.spec, key) if self.spec else ""
        return val or "—"

    def _meta_field(self, name: str) -> str:
        if not self.meta:
            return "读取中…"
        m = self.meta

        def first(*keys):
            for k in keys:
                v = m.get(k)
                if v not in (None, ""):
                    return str(v)
            return ""

        if name == "taken":
            return first("EXIF:DateTimeOriginal", "EXIF:CreateDate",
                         "QuickTime:CreateDate", "QuickTime:MediaCreateDate") or "—"
        if name == "device":
            make = first("QuickTime:Make", "EXIF:Make")
            model = first("QuickTime:Model", "EXIF:Model")
            return " · ".join(x for x in (make, model) if x) or "—"
        if name == "camera":
            make = first("EXIF:Make", "QuickTime:Make")
            model = first("EXIF:Model", "QuickTime:Model")
            return " · ".join(x for x in (make, model) if x) or "—"
        if name == "lens":
            return first("EXIF:LensModel", "EXIF:LensInfo", "XMP:Lens",
                         "EXIF:LensID") or "—"
        if name == "exposure":
            bits = []
            fn = first("EXIF:FNumber", "EXIF:ApertureValue")
            sh = first("EXIF:ExposureTime", "EXIF:ShutterSpeedValue")
            iso = first("EXIF:ISO", "EXIF:PhotographicSensitivity")
            fl = first("EXIF:FocalLength")
            if fn:
                bits.append(str(fn) if str(fn).startswith("f/") else f"f/{fn}")
            if sh:
                bits.append(f"{sh} 秒")
            if iso:
                bits.append(f"ISO {iso}")
            if fl:
                bits.append(str(fl))
            return " · ".join(bits) or "—"
        if name == "meter":
            return " · ".join(x for x in (first("EXIF:MeteringMode"),
                                          first("EXIF:Flash")) if x) or "—"
        if name == "size":
            w = first("EXIF:ExifImageWidth", "EXIF:ImageWidth", "File:ImageWidth",
                      "QuickTime:ImageWidth", "PNG:ImageWidth")
            h = first("EXIF:ExifImageHeight", "EXIF:ImageHeight", "File:ImageHeight",
                      "QuickTime:ImageHeight", "PNG:ImageHeight")
            return f"{w} × {h}" if w and h else "—"
        if name == "mp":
            w = first("EXIF:ExifImageWidth", "EXIF:ImageWidth", "File:ImageWidth")
            h = first("EXIF:ExifImageHeight", "EXIF:ImageHeight", "File:ImageHeight")
            try:
                mp = float(w) * float(h) / 1e6
                return f"{mp:.1f} 百万像素" if mp > 0 else "—"
            except Exception:
                return "—"
        if name == "color":
            cs = first("EXIF:ColorSpace", "ICC_Profile:ColorSpaceData")
            bd = first("EXIF:BitsPerSample", "PNG:BitDepth", "File:BitsPerSample")
            return " · ".join(x for x in (cs, (f"{bd} 位" if bd.isdigit() else bd))
                              if x) or "—"
        if name == "orientation":
            return first("EXIF:Orientation") or "—"
        if name == "software":
            return first("EXIF:Software", "XMP:CreatorTool") or "—"
        if name == "gps":
            return first("Composite:GPSPosition", "EXIF:GPSPosition",
                         "QuickTime:GPSCoordinates") or "—"
        return "—"

    # ---------------------------------------------------------------- 全部元数据
    def _tab_raw(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.raw_filter = QLineEdit()
        self.raw_filter.setPlaceholderText("搜索字段名或值…")
        self.raw_filter.setClearButtonEnabled(True)
        self.raw_filter.textChanged.connect(self._filter_raw)
        head.addWidget(self.raw_filter, 1)
        self.raw_count = QLabel("正在读取…")
        self.raw_count.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        head.addWidget(self.raw_count, 0)
        lay.addLayout(head)

        self.raw_tree = QTreeWidget()
        self.raw_tree.setColumnCount(2)
        self.raw_tree.setHeaderLabels(["字段", "值"])
        self.raw_tree.setRootIsDecorated(True)
        self.raw_tree.setUniformRowHeights(True)
        self.raw_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.raw_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.raw_tree.header().setStretchLastSection(True)
        self.raw_tree.setColumnWidth(0, 240)
        lay.addWidget(self.raw_tree, 1)
        return box

    def _fill_raw(self):
        self.raw_tree.clear()
        meta = self.meta
        if not meta:
            self.raw_count.setText("没读到元数据")
            return
        groups: dict[str, list[tuple[str, str]]] = {}
        for key, val in meta.items():
            group = key.split(":", 1)[0] if ":" in key else "Other"
            groups.setdefault(group, []).append((key, str(val)))
        names = sorted(groups.keys(),
                       key=lambda g: (_GROUP_ORDER.index(g)
                                      if g in _GROUP_ORDER else 99, g))
        for group in names:
            items = sorted(groups[group], key=lambda kv: kv[0])
            color = _GROUP_COLORS.get(group, T.TEXT_SUB)
            top = QTreeWidgetItem(self.raw_tree)
            top.setText(0, f"{_GROUP_CN.get(group, group)}（{len(items)}）")
            top.setFirstColumnSpanned(True)
            top.setForeground(0, QColor(color))
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            for key, val in items:
                from ..core import fields as F
                tag = key.split(":", 1)[-1]
                child = QTreeWidgetItem(top)
                child.setText(0, F.FIELD_CN.get(tag, tag))
                # v198: 界面里不显示东八时区; tooltip 仍保留 exiftool 原始值
                child.setText(1, F.display_value(val))
                child.setData(0, Qt.UserRole, key)
                child.setToolTip(0, key)
                child.setToolTip(1, val)
                child.setForeground(0, QColor(color))   # 字段名按来源分组上色
                child.setForeground(1, QColor(T.TEXT))  # 值统一深色
            top.setExpanded(True)
        self.raw_count.setText(f"共 {len(meta)} 项")

    def _filter_raw(self, text: str):
        text = (text or "").strip().lower()
        for i in range(self.raw_tree.topLevelItemCount()):
            top = self.raw_tree.topLevelItem(i)
            shown = 0
            for j in range(top.childCount()):
                child = top.child(j)
                raw = (child.data(0, Qt.UserRole) or "").lower()
                hit = (not text) or text in child.text(0).lower() \
                    or text in raw or text in child.text(1).lower()
                child.setHidden(not hit)
                shown += 1 if hit else 0
            top.setHidden(shown == 0)

    # ---------------------------------------------------------------- 异步读取
    def _start_workers(self):
        if not self.path or not os.path.exists(self.path):
            self.raw_count.setText("找不到源文件")
            self._probe_done = True
            for lab in self._value_labels.values():
                lab.setText("—")
            return
        self._meta_worker = _MetaWorker(self.path, parent=self)
        self._meta_worker.done.connect(self._on_meta)
        self._meta_worker.start()
        if self.kind in ("video", "audio"):
            from .props_dialog import _ProbeWorker
            self._probe_worker = _ProbeWorker([self.path], None, parent=self)
            self._probe_worker.done.connect(self._on_spec)
            self._probe_worker.start()

    def _on_spec(self, specs):
        self._probe_worker = None
        self._probe_done = True
        self.spec = (specs or {}).get(self.path) or {}
        self._refresh_detail()

    def _on_meta(self, _path, rec):
        self._meta_worker = None
        self.meta = rec or {}
        self._refresh_detail()
        self._fill_raw()
        if not self.meta:
            self.raw_count.setText("没读到元数据")

    def _refresh_detail(self):
        for key, lab in self._value_labels.items():
            text = self._detail_value(key)
            lab.setText(text)
            lab.setStyleSheet(
                f"color: {T.TEXT_WEAK if text in ('读取中…',) else T.TEXT};"
                " font-size: 12px;")

    def closeEvent(self, event):
        for name in ("_meta_worker", "_probe_worker"):
            w = getattr(self, name, None)
            if w is None:
                continue
            setattr(self, name, None)
            try:
                w.requestInterruption()
            except Exception:
                pass
            w.wait(3000)
        super().closeEvent(event)

    @staticmethod
    def _volume_text(clip) -> str:
        v = float(getattr(clip, "volume", 1.0) or 1.0)
        pct = int(round(v * 100))
        if pct <= 0:
            return "0%（静音）"
        from math import log10
        db = 20.0 * log10(max(v, 1e-6))
        if abs(db) < 0.05:
            return "100%（原样）"
        return f"{pct}%（{db:+.1f} dB）"

    @staticmethod
    def _sound_text(clip) -> str:
        if getattr(clip, "detached", False):
            return "声音已分离（搬到下面那条轨上了）"
        if getattr(clip, "muted", False):
            return "静音"
        return "正常出声"


class AudioSpeedDialog(_ModalBase):
    """音频的「帧率/倍速」: 只有倍速一格(音频没有帧率), 不生成新文件。

    跟视频那套「改帧率=无损变速」刻意分开: 音频没有帧, 变速必须重采样,
    做不到无损 —— 所以这里只把倍速记在片段上, 导出的时候才真正编码。
    原文件一个字节都不动, 随时可以再改、可以 Ctrl+Z。
    """

    def __init__(self, parent, name: str, speed: float, src_span: float):
        super().__init__(parent, "音频倍速", 480)
        self.name = name
        self.orig = max(0.05, min(20.0, float(speed or 1.0)))
        self.span = max(0.0, float(src_span or 0.0))

        head = QLabel(f"<b>{self.name}</b>")
        head.setTextFormat(Qt.RichText)
        self.root.addWidget(head)

        self.root.addWidget(_Card(
            "改快改慢，音调都不变（跟视频那套“连音调一起变”的无损变速不同）。\n"
            "代价：导出时这一段声音要重新编码，画面不受影响。"))

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("倍速"))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setDecimals(3)
        self.speed_spin.setRange(0.05, 20.0)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setFixedWidth(_spin_w(self.speed_spin, "20.000"))
        self.speed_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        unit = QLabel("×")
        unit.setStyleSheet(f"color: {T.TEXT_WEAK}; background: transparent;"
                           " border: none;")
        row.addWidget(self.speed_spin)
        row.addWidget(unit)
        row.addStretch(1)
        self.root.addLayout(row)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(QLabel("常用："))
        for mul in (0.5, 1.0, 2.0, 4.0, 8.0):
            b = QPushButton(f"{mul:g}×")
            b.setFixedHeight(27)
            b.setMinimumWidth(60)
            b.setStyleSheet("padding: 0 6px;")
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, m=mul: self.speed_spin.setValue(m))
            chips.addWidget(b)
        chips.addStretch(1)
        self.root.addLayout(chips)

        self.hint = _Card("")
        self.root.addWidget(self.hint)

        self.speed_spin.valueChanged.connect(self._refresh)
        self.speed_spin.setValue(self.orig)
        # 不写「生成新文件」: 这个工具**不**生成新文件, 直接改片段
        self._buttons("应用", self.accept, cancel=True)
        self._refresh()      # ok_btn 是 _buttons 才建的, 建完再核一遍灰亮

    def _refresh(self):
        v = self.speed_spin.value()
        # span 是**源文件**那段的长度, 片段现在占的时长 = span / 当前倍速
        before = (self.span / self.orig) if self.orig > 0 else self.span
        after = (self.span / v) if v > 0 else 0.0
        self.hint.setText(
            f"<b>{v:g}×</b>　时长：{_fmt(before)} → <b>{_fmt(after)}</b>")
        # 跟现在的值一样就不用点(点了也是白改一次)
        ok_btn = getattr(self, "ok_btn", None)
        if ok_btn is not None:
            ok_btn.setEnabled(abs(v - self.orig) > 1e-4)

    def value(self) -> float:
        return float(self.speed_spin.value())


class DecimateDialog(_ModalBase):
    """只留关键帧: 每 n 个留 1 个, 按目标帧率重新封装。"""

    def __init__(self, parent, name: str, fps: float, duration: float,
                 keyframes: int):
        super().__init__(parent, "大幅削减（只留关键帧）", 580)
        self.name = name
        self.fps = float(fps or 25.0)
        self.duration = float(duration or 0.0)
        self.kf_count = int(keyframes or 0)
        self._build()
        self._refresh()

    def _build(self):
        # v165: 片段名已经写在下面那句里了(【名字】), 上面不再单独占一行 ——
        # 同一个文件名出现两次是浪费, 页面上每一行都要有用。
        self.root.addWidget(_Card(
            "保留关键帧，删除中间帧。<br>"
            f"【<b>{self.name}</b>】精简为延时摄影（无需重编码），"
            "画质不会降低，但"
            f'<span style="color:{T.DANGER};">输出无声音</span>'))

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("每隔"))
        self.every_spin = QSpinBox()
        self.every_spin.setRange(1, 999)
        self.every_spin.setValue(1)
        self.every_spin.setFixedWidth(_spin_w(self.every_spin, "999"))
        self.every_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        row.addWidget(self.every_spin)
        row.addWidget(QLabel("个关键帧保留 1 个（1 = 全部保留）"))
        row.addStretch(1)
        self.root.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("输出帧率"))
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setDecimals(2)
        self.fps_spin.setRange(1.0, 240.0)
        self.fps_spin.setValue(self.fps)
        # ⚠️ 单位不做 suffix: 输入框里的字全选/双击会连 "fps" 一起选中。
        #    单位单独摆在外面(和「改帧率」那个窗一致)。
        self.fps_spin.setFixedWidth(_spin_w(self.fps_spin, "240.00"))
        self.fps_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        row2.addWidget(self.fps_spin)
        fps_unit = QLabel("fps")
        fps_unit.setStyleSheet(f"color: {T.TEXT_WEAK}; background: transparent;"
                               " border: none;")
        row2.addWidget(fps_unit)
        row2.addWidget(QLabel("（越高 → 播得越快、时长越短）"))
        row2.addStretch(1)
        self.root.addLayout(row2)

        self.hint = _Card("")
        self.root.addWidget(self.hint)
        self.every_spin.valueChanged.connect(lambda _v: self._refresh())
        self.fps_spin.valueChanged.connect(lambda _v: self._refresh())
        self._buttons("生成新文件", self.accept, cancel=False)

    def _refresh(self):
        every = max(1, self.every_spin.value())
        keep = (self.kf_count + every - 1) // every if self.kf_count else 0
        fps = self.fps_spin.value()
        after = (keep / fps) if fps > 0 else 0.0
        if self.kf_count:
            self.hint.setText(
                f"共 <b>{self.kf_count}</b> 个关键帧 → 保留 <b>{keep}</b> 帧<br>"
                f"时长：{_fmt(self.duration)} → <b>{_fmt(after)}</b>")
        else:
            self.hint.setText("这个文件读不到关键帧，先关掉这个窗口。")
        ok_btn = getattr(self, "ok_btn", None)
        if ok_btn is not None:
            ok_btn.setEnabled(self.kf_count > 0)

    def value(self) -> tuple[int, float]:
        return max(1, self.every_spin.value()), float(self.fps_spin.value())


class DetectDialog(_ModalBase):
    """四项检测 + 结果 + 一键处理。检测本身在主窗口的线程里跑。"""

    detectRequested = Signal(list)      # ["black", "silence", "scene", "keyframes"]
    actionRequested = Signal(str)       # drop_black / drop_silence / cut_scene
    stopped = Signal()

    KINDS = (("black", "检测黑色场景"), ("silence", "检测无声场景"),
             ("scene", "检测场景变化"), ("keyframes", "读取所有关键帧"))

    def __init__(self, parent, name: str, kf_count: int = 0):
        """kf_count = 这个素材已经读到几个关键帧(>0 时那一项置灰, 不用再读)。

        关键帧是加到轨道时就自动后台读的(v139), 绝大多数时候早就有了 ——
        这时候让用户再勾一次纯属多余(还得等 ffprobe 把整个文件扫一遍)。
        只有真没读到(文件超大/当时还在忙)才亮出来让他手动读。
        """
        super().__init__(parent, "检测", 620)
        self.name = name
        self.kf_count = int(kf_count or 0)
        self.result = None
        self._running = False
        self._build()

    def _build(self):
        head = QLabel(f"<b>{self.name}</b>")
        head.setTextFormat(Qt.RichText)
        self.root.addWidget(head)

        box = QFrame()
        box.setObjectName("toolCard")
        box.setStyleSheet(f"QFrame#toolCard {{ background: {T.SURFACE_ALT};"
                          f" border: 1px solid {T.BORDER}; border-radius: 8px; }}")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 9, 12, 9)
        bl.setSpacing(6)
        self.checks = {}
        for key, text in self.KINDS:
            # CheckCheckBox: 勾上时画蓝底 + 白勾(v136)
            # 普通 QCheckBox 在 QSS 下只有一个蓝色实心方块, 看不出"勾没勾"。
            if key == "keyframes" and self.kf_count > 0:
                # 已经自动读到 → 不必再读, 置灰并把数量摆出来
                text = f"关键帧：已读到 {self.kf_count} 个"
            cb = CheckCheckBox(text)
            cb.setChecked(True)
            if key == "keyframes" and self.kf_count > 0:
                cb.setChecked(False)
                cb.setEnabled(False)
                cb.setToolTip("加进轨道时已经自动读好了，Alt+← / Alt+→ 可以直接跳，"
                              "「对齐关键帧」「批量截图」也在用这同一份")
            self.checks[key] = cb
            bl.addWidget(cb)
        self.root.addWidget(box)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setVisible(False)
        self.root.addWidget(self.bar)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        self.log.setStyleSheet(
            f"background: {T.SURFACE_ALT}; border: 1px solid {T.BORDER};"
            " border-radius: 8px; font-size: 12px;")
        self.log.setPlaceholderText("勾选要检测的内容，然后点「开始检测」。")
        self.root.addWidget(self.log, 1)

        # v197: 「修复时长」的结论直接写在本窗口里, 不弹独立提示框、也不退出检测页。
        # 单独占一行, 免得被检测结果的日志覆盖掉。
        self.fix_status = QLabel("")
        self.fix_status.setWordWrap(True)
        self.fix_status.setStyleSheet(
            f"color: {T.TEXT}; background: {T.SURFACE_ALT};"
            f" border: 1px solid {T.BORDER}; border-radius: 6px; padding: 6px 8px;")
        self.fix_status.setVisible(False)
        self.root.addWidget(self.fix_status)

        # 底部(v136): 左边"对结果动手"的几个 + 修复时长, 右边只有「开始检测」。
        # 「关闭」删了 —— 右上角那个 ✕ 就是关闭, 多一个按钮纯属占地。
        row = QHBoxLayout()
        self.black_btn = QPushButton("删掉所有黑场段")
        self.black_btn.clicked.connect(lambda: self.actionRequested.emit("drop_black"))
        self.silence_btn = QPushButton("删掉所有无声段")
        self.silence_btn.clicked.connect(lambda: self.actionRequested.emit("drop_silence"))
        self.scene_btn = QPushButton("在场景变化处切开")
        self.scene_btn.clicked.connect(lambda: self.actionRequested.emit("cut_scene"))
        # 修复时长: 原来在工具条上单占一个按钮, 挪进来(v136) ——
        # 它本来就是"看看这个文件有没有问题", 和检测是同一类活。
        self.fix_btn = QPushButton("修复时长")
        self.fix_btn.setToolTip("核对文件里写的时长和实际内容长度；"
                                "不一致就重新封装一次（画面不重编码，原文件不动）")
        self.fix_btn.clicked.connect(
            lambda: self.actionRequested.emit("fix_duration"))
        for b in (self.black_btn, self.silence_btn, self.scene_btn):
            b.setEnabled(False)
            row.addWidget(b)
        row.addWidget(self.fix_btn)
        row.addStretch(1)

        self.start_btn = QPushButton("开始检测")
        self.start_btn.setProperty("accent", True)
        self.start_btn.setMinimumWidth(96)
        self.start_btn.clicked.connect(self._start)
        row.addWidget(self.start_btn)          # 居右
        self.root.addLayout(row)

    # ------------------------------------------------------------ 流程
    def _start(self):
        if self._running:
            return
        kinds = [k for k, cb in self.checks.items() if cb.isChecked()]
        if not kinds:
            self.log.setPlainText("至少勾一项再开始。")
            return
        self._running = True
        self.result = None
        self.start_btn.setEnabled(False)
        self.start_btn.setText("检测中…")
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.log.setPlainText("正在检测，长视频要等一会儿…")
        for b in (self.black_btn, self.silence_btn, self.scene_btn):
            b.setEnabled(False)
        self.detectRequested.emit(kinds)

    def set_progress(self, pct: int):
        self.bar.setValue(max(0, min(100, int(pct))))

    def set_result(self, result: dict):
        self._running = False
        self.result = result or {}
        self.bar.setValue(100)
        self.start_btn.setEnabled(True)
        self.start_btn.setText("重新检测")
        lines = []
        black = self.result.get("black") or []
        silence = self.result.get("silence") or []
        scene = self.result.get("scene") or []
        kfs = self.result.get("keyframes") or []
        if "black" in self.checks and self.checks["black"].isChecked():
            if black:
                lines.append(f"<b>黑场 {len(black)} 段</b>：" + "、".join(
                    f"{_fmt(b['start'])}→{_fmt(b['end'])}" for b in black[:8])
                    + ("…" if len(black) > 8 else ""))
            else:
                lines.append("黑场：没找到")
        if self.checks["silence"].isChecked():
            if silence:
                lines.append(f"<b>无声 {len(silence)} 段</b>：" + "、".join(
                    f"{_fmt(s['start'])}→{_fmt(s['end'])}" for s in silence[:8])
                    + ("…" if len(silence) > 8 else ""))
            else:
                lines.append("无声：没找到")
        if self.checks["scene"].isChecked():
            if scene:
                lines.append(f"<b>场景变化 {len(scene)} 处</b>：" + "、".join(
                    _fmt(s["t"]) for s in scene[:10])
                    + ("…" if len(scene) > 10 else ""))
            else:
                lines.append("场景变化：没找到（可以理解成画面一直很连贯）")
        if not self.checks["keyframes"].isEnabled() and self.kf_count > 0:
            # 这一趟没去读(早就有了) —— 别在结果里写"没读到", 那和上面的置灰自相矛盾
            lines.append(f"关键帧：<b>已有 {self.kf_count} 个</b>（加进轨道时就自动读好了）")
        elif self.checks["keyframes"].isChecked():
            lines.append(f"<b>关键帧 {len(kfs)} 个</b>（已经画到时间线上）"
                         if kfs else "关键帧：没读到")
        lines.append("<br><span style='color:%s'>结果已经标在时间线上："
                     "黑场灰、无声蓝、场景变化橙虚线。</span>" % T.TEXT_WEAK)
        self.log.setHtml("<div style='line-height:160%'>" + "<br>".join(lines) + "</div>")
        self.black_btn.setEnabled(bool(black))
        self.silence_btn.setEnabled(bool(silence))
        self.scene_btn.setEnabled(bool(scene))

    def set_error(self, msg: str):
        self._running = False
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始检测")
        self.bar.setVisible(False)
        self.log.setPlainText(msg or "检测失败")

    def set_fix_status(self, text: str):
        """v197: 把「修复时长」的结论显示在检测窗口内(不另开弹窗、不退出检测页)。"""
        self.fix_status.setText(text or "")
        self.fix_status.setVisible(bool(text))

    def reject(self):
        if self._running:
            self.stopped.emit()
        super().reject()


class ClipOutDialog(_ModalBase):
    """剪辑生成的文件存到哪。

    默认**不落在原文件夹**: 改帧率/削减/修时长/分离音频会多出一堆新文件,
    全堆在素材目录里, 原素材就找不着了。想放回去也行, 这里一句话就能改。
    """

    def __init__(self, parent=None):
        super().__init__(parent, "生成的文件存到哪", 560)
        from ..merge import settings
        self._settings = settings
        self._mode = settings.load_clip_out_mode()
        self._dir = settings.load_clip_out_dir()
        self._build()
        self._refresh()

    def _build(self):
        self.root.addWidget(_Card(
            "改帧率、大幅削减、修复时长、分离音频都会生成新文件（原文件一律不动）。\n"
            "默认存到单独的文件夹，素材目录就不会被这些半成品弄乱。"))

        # v149: 改成勾选式(✅ 三选一), 并且只留一个按钮 ——
        #       这是"挑一个再确定"的事, 不需要"取消"(右上角 × 一样能关)。
        self._sync = False
        self.r_sub = CheckCheckBox(
            f"原文件所在文件夹里新建一个「{self._settings.OUT_SUB_NAME}」（推荐）")
        self.r_same = CheckCheckBox("直接存在原文件旁边（和老版本一样）")
        self.r_custom = CheckCheckBox("存到我自己选的文件夹")
        self._boxes = (self.r_sub, self.r_same, self.r_custom)
        cur = {self._settings.OUT_SAME: self.r_same,
               self._settings.OUT_CUSTOM: self.r_custom}.get(self._mode, self.r_sub)
        cur.setChecked(True)
        for r in self._boxes:
            r.setCursor(Qt.PointingHandCursor)
            # 用 clicked(用户真的点了) 而不是 toggled: 由代码改状态时不该反过来再触发
            r.clicked.connect(lambda _checked=False, w=r: self._pick(w))
            self.root.addWidget(r)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QLineEdit(self._dir)
        self.edit.setReadOnly(True)
        self.edit.setPlaceholderText("还没选文件夹")
        self.browse_btn = QPushButton("浏览…")
        self.browse_btn.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(self.browse_btn, 0)
        self.root.addLayout(row)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        self.root.addWidget(self.hint)
        self.root.addStretch(1)
        self._buttons("保存", self.accept, cancel=False)

    # ---------------------------------------------------------------- 状态
    def _pick(self, who):
        """勾选式三选一: 点谁就只有谁打着勾(点已勾的那个也不会变成全不选)。"""
        if self._sync:
            return
        self._sync = True
        for r in self._boxes:
            r.setChecked(r is who)
        self._sync = False
        self._refresh()

    def _refresh(self):
        custom = self.r_custom.isChecked()
        self.edit.setEnabled(custom)
        self.browse_btn.setEnabled(custom)
        if custom:
            self.hint.setText("选好的文件夹会被记住，以后一直用这个。"
                              if self.edit.text().strip()
                              else "点「浏览…」挑一个文件夹。")
            self.ok_btn.setEnabled(bool(self.edit.text().strip()))
            return
        self.ok_btn.setEnabled(True)
        if self.r_same.isChecked():
            self.hint.setText("新文件会和原素材混在同一个文件夹里。")
        else:
            self.hint.setText(
                f"比如素材在 D:\\旅行，新文件就在 "
                f"D:\\旅行\\{self._settings.OUT_SUB_NAME} 里。")

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择存放新文件的文件夹", self.edit.text().strip() or "")
        if folder:
            self.edit.setText(folder)
            self._refresh()

    # ---------------------------------------------------------------- 保存
    def save(self):
        mode = (self._settings.OUT_SAME if self.r_same.isChecked()
                else self._settings.OUT_CUSTOM if self.r_custom.isChecked()
                else self._settings.OUT_SUB)
        self._settings.save_clip_out_mode(mode)
        if mode == self._settings.OUT_CUSTOM:
            self._settings.save_clip_out_dir(self.edit.text().strip())
        return mode
