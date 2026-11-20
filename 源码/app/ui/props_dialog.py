# -*- coding: utf-8 -*-
"""右键「属性」对话框。

Windows 自带的属性只给大小/日期/分辨率/时长几项, 这里把能拿到的都用上:
  ① 常规  —— 缩略图 + 文件系统信息 + 本软件内的排序/计划/合并状态
  ② 拍摄 / 视频 —— 图片看相机与曝光, 视频看编码与音轨(按文件类型自动切换)
  ③ 全部元数据 —— 原始 EXIF/XMP/QuickTime 全量表格(可搜索)

多选时逐字段汇总: 值一致就显示该值, 不一致显示「混合」。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog,
                               QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from . import theme as T
from ..core import fields as F
from ..core.photo import STATUS_DONE, STATUS_ERROR, STATUS_PENDING
from ..merge import grouping

MIXED = "混合"
DASH = "—"

# 致命参数名常驻标色: 硬致命红 / 软致命橙, 其余不标(空串)。供视频页参数名着色。
_SPEC_LABEL_COLORS = {k: (T.DANGER if lv == "fatal" else T.WARN)
                      for k, _cn, lv in grouping.FIELD_LEVELS if lv}

# 常见键的候选列表(不同相机/容器写法不一样, 依次取第一个有值的)
K_WIDTH = ("EXIF:ExifImageWidth", "EXIF:ImageWidth", "File:ImageWidth",
           "QuickTime:ImageWidth", "Track1:ImageWidth", "PNG:ImageWidth",
           "Composite:ImageWidth")
K_HEIGHT = ("EXIF:ExifImageHeight", "EXIF:ImageHeight", "File:ImageHeight",
            "QuickTime:ImageHeight", "Track1:ImageHeight", "PNG:ImageHeight",
            "Composite:ImageHeight")
K_DURATION = ("QuickTime:Duration", "QuickTime:MediaDuration", "Composite:Duration")
K_FPS = ("QuickTime:VideoFrameRate", "QuickTime:FrameRate", "Composite:VideoFrameRate")
K_VCODEC = ("QuickTime:CompressorID", "QuickTime:VideoCodecID", "Composite:VideoCodec")
K_BITRATE = ("QuickTime:AvgBitrate", "Composite:AvgBitrate", "QuickTime:BitRate")
K_ACODEC = ("QuickTime:AudioFormat", "QuickTime:AudioCodecID", "Composite:AudioCodec")
K_CHANNELS = ("QuickTime:AudioChannels", "QuickTime:ChannelCount", "Composite:AudioChannels")
K_RATE = ("QuickTime:AudioSampleRate", "Composite:AudioSampleRate")
K_MODIFY = ("File:FileModifyDate",)
K_CREATE = ("File:FileCreateDate", "EXIF:CreateDate")
K_GPS = ("Composite:GPSPosition", "EXIF:GPSPosition")


def _first(meta: dict, keys) -> str:
    for k in keys:
        v = meta.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _fmt_size(n: int) -> str:
    if not n or n <= 0:
        return DASH
    units = ("B", "KB", "MB", "GB", "TB")
    v, i = float(n), 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.2f} {units[i]}（{n:,} 字节）"


def _agg(values, dash=DASH) -> str:
    """一组值: 全空 -> dash; 唯一 -> 该值; 多个 -> 混合。"""
    uniq = {v for v in values if v}
    if not uniq:
        return dash
    if len(uniq) == 1:
        return next(iter(uniq))
    return MIXED


def _field(photos, keys, dash=DASH) -> str:
    return _agg([_first(p.meta, keys) for p in photos], dash)


def _resolution(photos) -> str:
    out = []
    for p in photos:
        w = _first(p.meta, K_WIDTH)
        h = _first(p.meta, K_HEIGHT)
        out.append(f"{w} × {h}" if w and h else "")
    return _agg(out)


def _megapixels(photos) -> str:
    out = []
    for p in photos:
        try:
            w = int(float(_first(p.meta, K_WIDTH)))
            h = int(float(_first(p.meta, K_HEIGHT)))
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            out.append(f"{w * h / 1e6:.1f} 百万像素")
    return _agg(out)


def _duration(photos) -> str:
    out = []
    for p in photos:
        raw = _first(p.meta, K_DURATION)
        if not raw:
            continue
        try:
            secs = float(raw.replace(",", "."))
        except ValueError:
            out.append(raw)
            continue
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = secs - h * 3600 - m * 60
        out.append(f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}")
    return _agg(out)


def _pretty_dt(text: str) -> str:
    """把 exiftool 的 2024:08:15 14:30:22+08:00 变成 2024-08-15 14:30:22。

    v198: 界面里不再显示东八时区(用户要求哪里都不显示), 只留北京时间主体;
    写入格式由 fields.format_for 决定, 这里一个字节都不影响写回。
    """
    dt = F.parse_dt(text or "")
    if not dt:
        return F.display_value(text or "")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _stat_time(photo, attr: str) -> str:
    try:
        ts = getattr(os.stat(photo.path), attr, 0)
    except OSError:
        return ""
    return _pretty_dt(datetime.fromtimestamp(ts).strftime("%Y:%m:%d %H:%M:%S"))


def _time_span(photos, keys, fallback_stat: str = "") -> str:
    """文件时间: 全一致显示一个; 不同显示 "最早 ~ 最晚"。"""
    real = [_pretty_dt(_first(p.meta, keys)) for p in photos]
    real = [v for v in real if v]
    if not real and fallback_stat:
        real = [v for v in (_stat_time(p, fallback_stat) for p in photos) if v]
    if not real:
        return DASH
    if len(set(real)) == 1:
        return real[0]
    parsed = []
    for v in real:
        dt = F.parse_dt(v.replace("（", " ").replace("）", ""))
        parsed.append((dt, v))
    parsed.sort(key=lambda t: (t[0] is None, t[0] or 0, t[1]))
    return f"{parsed[0][1]} ~ {parsed[-1][1]}"


def _type_text(photos) -> str:
    kinds = {p.kind for p in photos}
    if len(photos) == 1:
        p = photos[0]
        kind = "图片" if p.kind == "image" else ("视频" if p.kind == "video" else "文件")
        fmt = _first(p.meta, ("File:FileType",))
        name = f"{fmt} {kind}" if fmt else kind
        ext = os.path.splitext(p.name)[1].lstrip(".")
        return f"{name}（.{ext}）" if ext else name
    parts = []
    if "image" in kinds:
        parts.append("图片")
    if "video" in kinds:
        parts.append("视频")
    if not parts:
        parts.append("文件")
    base = " + ".join(parts)
    return base if len(kinds) <= 1 else f"{MIXED} · {base}"


def _status_text(photo) -> str:
    if photo.status == STATUS_PENDING:
        return "待写入"
    if photo.status == STATUS_DONE:
        return "已写入"
    if photo.status == STATUS_ERROR:
        return "出错" + (f"：{photo.error}" if photo.error else "")
    return "无改动"


def _reveal(path: str):
    path = os.path.normpath(path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


class _Rows:
    """先攒「分区 + 字段 + 值」, 再一次性排版; 复制文本也用它。

    items 每项 = (分区, 参数名, 值, 字段 key)。key 非空的行是「由 ffprobe 异步回填」
    的规格行: 参数名常驻标色, 值等 spec 到达后由 set_value() 更新。
    """

    def __init__(self):
        self.items: list[tuple[str, str, str, str]] = []
        self._sec = ""

    def section(self, name: str):
        self._sec = name

    def add(self, label: str, value: str, color: str = "", key: str = ""):
        self.items.append((self._sec, label or "", str(value if value else DASH), key))

    def set_value(self, key: str, value: str):
        for i, (sec, label, _v, k) in enumerate(self.items):
            if k == key:
                self.items[i] = (sec, label, str(value if value else DASH), k)
                return

    def text(self) -> str:
        out, cur = [], None
        for sec, label, value, _k in self.items:
            if sec != cur:
                if out:
                    out.append("")
                out.append(f"【{sec}】")
                cur = sec
            out.append(f"{label}：{value}" if label else value)
        return "\n".join(out)


def _round_pixmap(pix: QPixmap, size: int, radius: int = 10) -> QPixmap:
    """把缩略图裁成圆角方块(铺满、居中裁切)。"""
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setClipPath(path)
    scaled = pix.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                        Qt.SmoothTransformation)
    painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2,
                       scaled)
    painter.end()
    return out


def _hsep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {T.BORDER}; border: none;")
    return line


class _ProbeWorker(QThread):
    """后台 ffprobe 探测视频规格, 供属性窗口「合并高危参数」使用。

    只在需要时启动一次; 结果 dict 通过 done 信号回主线程。
    """
    done = Signal(object)     # {path: spec}

    def __init__(self, paths, ffprobe_hint=None, parent=None):
        super().__init__(parent)
        self.paths = list(paths)
        self.ffprobe_hint = ffprobe_hint

    def run(self):
        from ..merge import ffmpeg_locate, probe
        ffprobe = self.ffprobe_hint
        if not ffprobe:
            try:
                pair = ffmpeg_locate.find_ffmpeg_pair()
            except Exception:
                pair = None
            ffprobe = pair[1] if pair else None
        out: dict[str, dict] = {}
        for p in self.paths:
            if not ffprobe:
                break
            try:
                spec, _err = probe.probe_ex(p, ffprobe, with_bitrate_mode=True)
            except Exception:
                spec = None
            if spec:
                out[p] = spec
        self.done.emit(out)


class PropertiesDialog(QDialog):
    """属性窗口: photos 可为多选; ctx 提供本软件内的额外信息。"""

    def __init__(self, photos, ctx: dict | None = None, parent=None):
        super().__init__(parent)
        self.photos = list(photos or [])
        self.ctx = ctx or {}
        title = "属性"
        if len(self.photos) == 1:
            title = f"属性 — {self.photos[0].name}"
        elif self.photos:
            title = f"属性 — 已选 {len(self.photos)} 个文件"
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setMinimumSize(620, 460)
        self.resize(720, 560)
        self._copy_texts: dict[int, str] = {}
        self._video_specs: dict[str, dict] = {}
        self._probe_worker: _ProbeWorker | None = None
        self._spec_value_labels: dict[str, QLabel] = {}
        self._rows_video: _Rows | None = None
        self._build()
        if self._only_video():
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._start_video_probe)

    # ---------------------------------------------------------------- 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        root.addWidget(self._header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_general(), "常规")
        self.tabs.addTab(self._tab_detail(), "视频信息" if self._only_video() else "拍摄信息")
        self.tabs.addTab(self._tab_raw(), "全部元数据")
        root.addWidget(self.tabs, 1)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.copy_btn = QPushButton("复制本页信息")
        self.copy_btn.clicked.connect(self._copy_current)
        bar.addWidget(self.copy_btn)
        bar.addStretch(1)
        self.open_btn = QPushButton("打开文件")
        self.open_btn.clicked.connect(self._open_file)
        self.open_btn.setEnabled(len(self.photos) == 1)
        bar.addWidget(self.open_btn)
        self.reveal_btn = QPushButton("打开所在文件夹")
        self.reveal_btn.clicked.connect(self._reveal_file)
        bar.addWidget(self.reveal_btn)
        root.addLayout(bar)

    def _only_video(self) -> bool:
        return bool(self.photos) and all(p.kind == "video" for p in self.photos)

    def _header(self) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # 预览图: 点一下用系统默认程序打开(图片=看图, 视频=播放器)
        thumb = QLabel()
        thumb.setFixedSize(76, 76)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet(f"background: {T.THUMB_BG}; border-radius: 10px;"
                            f" color: {T.TEXT_WEAK}; font-size: 11px;")
        pix = QPixmap()
        src = self.photos[0].thumb if self.photos else None
        if src is not None and not src.isNull():
            pix = src
        if pix.isNull():
            thumb.setText("无预览")
        else:
            thumb.setPixmap(_round_pixmap(pix, 72))
        if len(self.photos) == 1:
            thumb.setCursor(Qt.PointingHandCursor)
            thumb.setToolTip("用默认程序打开")
            thumb.mouseReleaseEvent = lambda ev, p=self.photos[0]: self._open_path(p)
        lay.addWidget(thumb, 0, Qt.AlignVCenter)

        # 只放文件名 —— 类型/大小/路径在「常规」里都有, 这里不重复
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)
        name = QLabel()
        if len(self.photos) == 1:
            name.setText(self.photos[0].name)
        else:
            name.setText(f"已选 {len(self.photos)} 个文件")
        name.setStyleSheet(f"color: {T.TEXT}; font-size: 15px; font-weight: 600;")
        name.setWordWrap(True)
        name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(name)
        info.addStretch(1)

        lay.addLayout(info, 1)
        return box

    def _total_size(self) -> int:
        total = 0
        for p in self.photos:
            if p.size:
                total += p.size
                continue
            try:
                total += os.path.getsize(p.path)
            except OSError:
                pass
        return total

    def _location(self) -> str:
        if not self.photos:
            return ""
        dirs = {os.path.dirname(p.path) for p in self.photos}
        if len(dirs) == 1:
            return next(iter(dirs))
        return f"（{len(dirs)} 个不同文件夹）"

    # ---------------------------------------------------------------- 常规
    def _tab_general(self) -> QWidget:
        rows = _Rows()
        rows.section("文件")
        if len(self.photos) == 1:
            rows.add("完整路径", self.photos[0].path)
        else:
            rows.add("位置", self._location())
        rows.add("大小", _fmt_size(self._total_size())
                 + (f"（{len(self.photos)} 个文件合计）" if len(self.photos) > 1 else ""))
        rows.add("类型", _type_text(self.photos))
        rows.add("修改时间", _time_span(self.photos, K_MODIFY, "st_mtime"))
        rows.add("创建时间", _time_span(self.photos, K_CREATE, "st_ctime"))

        rows.section("在本软件里")
        rows.add("排序位置", self._order_text())
        rows.add("计划时间", self._plan_text())
        rows.add("状态", self._status_summary())
        merge = self._merge_text()
        if merge:
            rows.add("合并分组", merge)

        self._copy_texts[0] = rows.text()
        return self._wrap_scroll(self._render(rows.items))

    def _order_text(self) -> str:
        order = self.ctx.get("order") or {}
        total = self.ctx.get("total") or 0
        pos = [order.get(p.path) for p in self.photos if order.get(p.path)]
        if not pos:
            return DASH
        if len(pos) == 1:
            return f"第 {pos[0]} 位" + (f" / 共 {total} 个" if total else "")
        lo, hi = min(pos), max(pos)
        return f"第 {lo} ~ {hi} 位 / 共 {total} 个" if total else f"第 {lo} ~ {hi} 位"

    def _plan_text(self) -> str:
        if len(self.photos) == 1:
            p = self.photos[0]
            planned = p.planned.get(F.TAKEN)
            if planned:
                return planned.strftime("%Y-%m-%d %H:%M:%S") + "（待写入）"
            if p.pending:
                return f"{len(p.pending)} 个字段待写入"
            return "无改动"
        dirty = sum(1 for p in self.photos if p.dirty)
        if not dirty:
            return "无改动"
        return f"{dirty} 个待写入 / {len(self.photos) - dirty} 个无改动"

    def _status_summary(self) -> str:
        if len(self.photos) == 1:
            return _status_text(self.photos[0])
        counts: dict[str, int] = {}
        for p in self.photos:
            key = _status_text(p)
            counts[key] = counts.get(key, 0) + 1
        return " · ".join(f"{k} {v} 个" for k, v in counts.items())

    def _merge_text(self) -> str:
        groups = self.ctx.get("merge_groups")
        if not groups:
            return ""
        want = {id(p) for p in self.photos}
        parts = []
        for g in groups:
            members = [p for p in (g.get("photos") or []) if id(p) in want]
            if not members:
                continue
            total = len(g.get("photos") or [])
            idx = [i + 1 for i, p in enumerate(g.get("photos") or []) if id(p) in want]
            tag = "可无损合并" if g.get("tag") == "lossless" else (
                "需转码" if g.get("tag") == "transcode" else "独立一段")
            rng = (f"第 {idx[0]} 段" if len(idx) == 1
                   else f"第 {idx[0]}~{idx[-1]} 段")
            label = g.get("label") or ""
            head = label if tag in label else f"{label}　{tag}".strip()
            parts.append(f"{head} · {rng} / 共 {total} 段")
        return "；".join(parts)

    # ---------------------------------------------------------------- 详细
    def _tab_detail(self) -> QWidget:
        rows = _Rows()
        video = bool(self.photos) and all(p.kind == "video" for p in self.photos)
        if video:
            self._fill_video(rows)
        elif all(p.kind == "image" for p in self.photos):
            self._fill_image(rows)
        else:
            rows.section("提示")
            rows.add("", "选中项里既有图片也有视频，请分别查看。")
        self._copy_texts[1] = rows.text()
        if video:
            self._rows_video = rows
            page = self._render_two_col(rows.items, ("视频", "音频"))
            return self._wrap_scroll(page)
        if all(p.kind == "image" for p in self.photos):
            return self._wrap_scroll(self._render_two_col(rows.items, ("拍摄", "画面")))
        return self._wrap_scroll(self._render(rows.items))

    def _start_video_probe(self):
        if self._probe_worker is not None:
            return
        paths = [p.path for p in self.photos if p.kind == "video"]
        if not paths:
            return
        hint = None
        if self.ctx:
            hint = self.ctx.get("ffprobe")
        self._probe_worker = _ProbeWorker(paths, hint, parent=self)
        self._probe_worker.done.connect(self._on_video_specs)
        self._probe_worker.start()

    def _on_video_specs(self, specs: dict):
        self._video_specs = dict(specs or {})
        self._probe_worker = None
        self._update_spec_rows()

    def _update_spec_rows(self):
        """ffprobe 结果到达后, 把视频/音频板块里 key 行的值刷新(参数名标色常驻)。"""
        specs = [s for s in self._video_specs.values() if s]
        # v198: 「码率类型」不进 FIELD_LEVELS(估计值不能拿去卡无损判定),
        # 但属性页要按同一套异步回填机制显示, 所以额外加进遍历。
        keys = [k for k, _cn, _lv in grouping.FIELD_LEVELS] + ["bitrate_mode"]
        for key in keys:
            if key not in self._spec_value_labels:
                continue
            if not specs:
                text = DASH
            else:
                vals = []
                for s in specs:
                    v = grouping.field_value(s, key) or "无"
                    if v not in vals:
                        vals.append(v)
                if vals == ["无"] or (key == "rotation" and vals == ["0°"]) \
                        or (key == "field_order" and vals == ["逐行"]):
                    text = DASH
                else:
                    text = " / ".join(vals)
            lab = self._spec_value_labels[key]
            lab.setText(text)
            lab.setStyleSheet(f"color: {self._value_color(text)}; font-size: 13px;")
            if self._rows_video:
                self._rows_video.set_value(key, text)
        if self._rows_video:
            self._copy_texts[1] = self._rows_video.text()

    def closeEvent(self, event):
        w = self._probe_worker
        if w is not None:
            self._probe_worker = None
            try:
                w.done.disconnect(self._on_video_specs)
            except Exception:
                pass
            w.requestInterruption()
            w.wait(3000)
        super().closeEvent(event)

    def _fill_image(self, rows: _Rows):
        rows.section("拍摄")
        rows.add("拍摄时间", _agg([p.synthetic_display(F.TAKEN) for p in self.photos]))
        rows.add("相机", _agg([
            f"{_first(p.meta, ('EXIF:Make',))} · {_first(p.meta, ('EXIF:Model',))}".strip(" ·")
            for p in self.photos]))
        rows.add("镜头", _field(self.photos, ("EXIF:LensModel", "EXIF:LensInfo",
                                              "XMP:Lens", "EXIF:LensID")))
        rows.add("曝光", _agg([self._exposure(p) for p in self.photos]))
        rows.add("曝光补偿", _field(self.photos, ("EXIF:ExposureCompensation",)))
        rows.add("测光 / 闪光", _agg([self._meter_flash(p) for p in self.photos]))

        rows.section("画面")
        rows.add("分辨率", _resolution(self.photos))
        rows.add("像素", _megapixels(self.photos))
        rows.add("色彩 / 位深", _agg([self._color_depth(p) for p in self.photos]))
        rows.add("方向", _field(self.photos, ("EXIF:Orientation",)))
        rows.add("处理软件", _field(self.photos, ("EXIF:Software", "XMP:CreatorTool")))

        rows.section("位置")
        rows.add("GPS", _field(self.photos, K_GPS))
        rows.add("海拔", _field(self.photos, ("EXIF:GPSAltitude", "Composite:GPSAltitude")))
        rows.add("城市 / 国家", _agg([
            " / ".join(x for x in (_first(p.meta, ("EXIF:City", "XMP:City")),
                                   _first(p.meta, ("EXIF:Country", "XMP:Country"))) if x)
            for p in self.photos]))

    def _exposure(self, photo) -> str:
        m = photo.meta
        fn = _first(m, ("EXIF:FNumber", "EXIF:ApertureValue"))
        sh = _first(m, ("EXIF:ExposureTime", "EXIF:ShutterSpeedValue"))
        iso = _first(m, ("EXIF:ISO", "EXIF:PhotographicSensitivity", "EXIF:ISOSpeedRatings"))
        fl = _first(m, ("EXIF:FocalLength",))
        bits = []
        if fn:
            bits.append(f"f/{fn}" if not str(fn).startswith("f/") else str(fn))
        if sh:
            bits.append(f"{sh} s" if "/" in str(sh) or str(sh).replace(".", "").isdigit() else str(sh))
        if iso:
            bits.append(f"ISO {iso}")
        if fl:
            bits.append(str(fl))
        return " · ".join(bits)

    def _meter_flash(self, photo) -> str:
        m = photo.meta
        me = _first(m, ("EXIF:MeteringMode",))
        fl = _first(m, ("EXIF:Flash",))
        return " · ".join(x for x in (me, fl) if x)

    def _color_depth(self, photo) -> str:
        m = photo.meta
        cs = _first(m, ("EXIF:ColorSpace", "ICC_Profile:ColorSpaceData"))
        bd = _first(m, ("EXIF:BitsPerSample", "PNG:BitDepth", "File:BitsPerSample"))
        return " · ".join(x for x in (cs, (f"{bd} 位" if bd and bd.isdigit() else bd)) if x)

    def _fill_video(self, rows: _Rows):
        # 致命参数并入「视频」「音频」两个板块: 参数名常驻红/橙(key 由 ffprobe 回填)
        rows.section("视频")
        rows.add("时长", _duration(self.photos))
        rows.add("分辨率", "", key="width")
        rows.add("帧率", "", key="r_frame_rate")
        rows.add("编码", "", key="video_codec")
        rows.add("像素格式", "", key="pix_fmt")
        rows.add("位深", "", key="bits")
        rows.add("编码档次", "", key="profile")
        rows.add("编码等级", "", key="level")
        rows.add("HDR 传输", "", key="color_transfer")
        rows.add("扫描方式", "", key="field_order")
        rows.add("时间基", "", key="time_base")
        rows.add("像素宽高比", "", key="sar")
        rows.add("色彩范围", "", key="color_range")
        rows.add("色彩空间", "", key="color_space")
        rows.add("三原色", "", key="color_primaries")
        rows.add("旋转", "", key="rotation")
        rows.add("码率", _field(self.photos, K_BITRATE))
        rows.add("码率类型", "", key="bitrate_mode")

        rows.section("音频")
        rows.add("音频编码", "", key="audio_codec")
        rows.add("音频档次", "", key="audio_profile")
        rows.add("音频采样格式", "", key="sample_fmt")
        rows.add("采样率", "", key="sample_rate")
        rows.add("声道数", "", key="channels")
        rows.add("声道布局", "", key="channel_layout")
        rows.add("音轨数", self._track_count())

        rows.section("其它")
        rows.add("流数量", "", key="nb_streams")
        rows.add("容器格式", "", key="format_name")
        rows.add("拍摄时间", _agg([p.synthetic_display(F.TAKEN) for p in self.photos]))
        rows.add("设备", _agg([
            f"{_first(p.meta, ('QuickTime:Make', 'EXIF:Make'))} · "
            f"{_first(p.meta, ('QuickTime:Model', 'EXIF:Model'))}".strip(" ·")
            for p in self.photos]))
        rows.add("GPS", _field(self.photos, K_GPS))

    def _fps(self, photo) -> str:
        v = _first(photo.meta, K_FPS)
        if not v:
            return ""
        try:
            return f"{float(v):g} fps"
        except ValueError:
            return v

    def _audio(self, photo) -> str:
        ch = _first(photo.meta, K_CHANNELS)
        sr = _first(photo.meta, K_RATE)
        bits = []
        if ch:
            bits.append(f"{ch} 声道")
        if sr:
            try:
                bits.append(f"{int(float(sr)) / 1000:g} kHz")
            except ValueError:
                bits.append(sr)
        return " · ".join(bits)

    def _track_count(self) -> str:
        counts = []
        for p in self.photos:
            n = 0
            while f"Track{n + 1}:TrackType" in p.meta:
                n += 1
            counts.append(str(n) if n else "")
        return _agg(counts)

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
        count = QLabel(self._raw_count_text())
        count.setStyleSheet(f"color: {T.TEXT_WEAK}; font-size: 12px;")
        head.addWidget(count, 0)
        lay.addLayout(head)

        self.raw_tree = QTreeWidget()
        self.raw_tree.setColumnCount(2)
        self.raw_tree.setHeaderLabels(["字段", "值"])
        self.raw_tree.setRootIsDecorated(True)
        self.raw_tree.setAlternatingRowColors(False)
        self.raw_tree.setUniformRowHeights(True)
        self.raw_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.raw_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.raw_tree.header().setStretchLastSection(True)
        self.raw_tree.setColumnWidth(0, 250)
        lay.addWidget(self.raw_tree, 1)
        self._fill_raw()
        self._copy_texts[2] = self._raw_text()
        return box

    def _merged_meta(self) -> dict[str, str]:
        """多选合并: 值一致保留, 不一致标「（多个不同值）」。"""
        acc: dict[str, list[str]] = {}
        for p in self.photos:
            for k, v in p.meta.items():
                if k == "SourceFile":
                    continue
                acc.setdefault(k, []).append(str(v))
        out = {}
        for k, vals in acc.items():
            uniq = {v for v in vals if v}
            out[k] = next(iter(uniq)) if len(uniq) == 1 else (
                "（多个不同值）" if len(uniq) > 1 else "")
        return out

    def _raw_count_text(self) -> str:
        meta = self._merged_meta()
        return f"共 {len(meta)} 项"

    def _fill_raw(self):
        self.raw_tree.clear()
        meta = self._merged_meta()
        groups: dict[str, list[tuple[str, str]]] = {}
        for key, val in meta.items():
            group = key.split(":", 1)[0] if ":" in key else "其它"
            groups.setdefault(group, []).append((key, val))
        order = ["EXIF", "XMP", "IPTC", "QuickTime", "Track", "Media", "GPS",
                 "Keys", "ItemList", "UserData", "PNG", "JFIF", "File", "Composite"]
        names = sorted(groups.keys(),
                       key=lambda g: (order.index(g) if g in order else 99, g))
        for group in names:
            items = sorted(groups[group],
                           key=lambda kv: F.group_sort_key(kv[0]) if hasattr(F, "group_sort_key") else kv[0])
            top = QTreeWidgetItem(self.raw_tree)
            cn = F.GROUP_CN.get(group, group)
            top.setText(0, f"{cn}（{len(items)}）")
            top.setFirstColumnSpanned(True)
            top.setForeground(0, QColor(T.TEXT_SUB))
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            for key, val in items:
                # 字段名能准确翻译就用中文(与右侧栏「元数据」同一张表), 原始 key 放 tooltip
                tag = key.split(":", 1)[-1]
                child = QTreeWidgetItem(top)
                child.setText(0, F.FIELD_CN.get(tag, tag))
                # v198: 界面里不显示东八时区; tooltip 仍保留 exiftool 原始值备查
                child.setText(1, F.display_value(val))
                child.setData(0, Qt.UserRole, key)
                child.setToolTip(0, key)
                child.setToolTip(1, val)
                child.setForeground(0, QColor(T.TEXT_SUB))
                child.setForeground(1, QColor(T.TEXT))
            top.setExpanded(True)

    def _filter_raw(self, text: str):
        text = text.strip().lower()
        for i in range(self.raw_tree.topLevelItemCount()):
            top = self.raw_tree.topLevelItem(i)
            shown = 0
            for j in range(top.childCount()):
                child = top.child(j)
                raw = (child.data(0, Qt.UserRole) or "").lower()   # 原始英文 key 也能搜
                hit = (not text) or text in child.text(0).lower() \
                    or text in raw or text in child.text(1).lower()
                child.setHidden(not hit)
                shown += 1 if hit else 0
            top.setHidden(shown == 0)

    def _raw_text(self) -> str:
        """复制出去用中文名(和界面一致), 括号里保留原始 key 方便对照。"""
        meta = self._merged_meta()
        out = []
        for k, v in sorted(meta.items()):
            tag = k.split(":", 1)[-1]
            cn = F.FIELD_CN.get(tag)
            out.append(f"{cn}（{k}）: {v}" if cn else f"{k}: {v}")
        return "\n".join(out)

    @staticmethod
    def _wrap_scroll(page: QWidget) -> QWidget:
        """内容可能超过窗口高度(视频页有 16 行高危参数), 包一层滚动区。"""
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setWidget(page)
        return sa

    # ---------------------------------------------------------------- 排版
    @staticmethod
    def _value_color(value: str) -> str:
        if value == MIXED:
            return T.WARN
        if "（待写入）" in value or value == "待写入":
            return T.ACCENT
        if value.startswith("出错"):
            return T.DANGER
        return T.TEXT

    def _section_box(self, title: str, rows, spacing: int = 6) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        if title:
            lab = QLabel(title)
            lab.setStyleSheet(f"color: {T.TEXT}; font-size: 13px; font-weight: 600;")
            lay.addWidget(lab)
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(spacing)
        grid.setColumnMinimumWidth(0, 70)
        grid.setColumnStretch(1, 1)
        for r, (label, value, key) in enumerate(rows):
            if label:
                tag = QLabel(label)
                tag.setStyleSheet(
                    f"color: {_SPEC_LABEL_COLORS.get(key, T.TEXT_WEAK)}; font-size: 12px;")
                tag.setAlignment(Qt.AlignRight | Qt.AlignTop)
                grid.addWidget(tag, r, 0)
            val = QLabel(value)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setStyleSheet(
                f"color: {self._value_color(value)}; font-size: 13px;")
            if key == "bitrate_mode":
                val.setToolTip("按前 20 秒逐秒码率的波动估计；文件里通常没有写死 CBR/VBR 标记。")
            grid.addWidget(val, r, 1)
            if key:
                self._spec_value_labels[key] = val
        lay.addWidget(holder)
        lay.addStretch(1)      # 富余高度留给 stretch, 内容始终顶部对齐
        return box

    def _render(self, items) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(13, 10, 13, 10)
        outer.setSpacing(9)
        groups: dict[str, list] = {}
        for sec, label, value, key in items:
            groups.setdefault(sec, []).append((label, value, key))
        first = True
        for sec, rows in groups.items():
            if not first:
                outer.addWidget(_hsep())
            outer.addWidget(self._section_box(sec, rows))
            first = False
        outer.addStretch(1)
        return page

    def _render_two_col(self, items, pair=("视频", "音频")) -> QWidget:
        """视频页: 「视频」「音频」并排, 其余分区在下方整行。横向空间用满。"""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(13, 10, 13, 10)
        outer.setSpacing(10)
        groups: dict[str, list] = {}
        for sec, label, value, key in items:
            groups.setdefault(sec, []).append((label, value, key))
        top = QHBoxLayout()
        top.setSpacing(26)
        for name in pair:
            rows = groups.pop(name, None)
            if rows:
                top.addWidget(self._section_box(name, rows, spacing=5), 1)
        if top.count():
            outer.addLayout(top)
        for sec, rows in groups.items():
            outer.addWidget(_hsep())
            outer.addWidget(self._section_box(sec, rows, spacing=5))
        outer.addStretch(1)
        return page

    # ---------------------------------------------------------------- 动作
    def _copy_current(self):
        idx = self.tabs.currentIndex()
        text = self._copy_texts.get(idx, "")
        if not text:
            return
        if len(self.photos) > 1:
            text = f"已选 {len(self.photos)} 个文件\n{text}"
        QApplication.clipboard().setText(text)
        self.copy_btn.setText("已复制")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1200, lambda: self.copy_btn.setText("复制本页信息"))

    def _open_file(self):
        if len(self.photos) != 1:
            return
        self._open_path(self.photos[0])

    @staticmethod
    def _open_path(photo):
        """用系统默认程序打开(图片=看图, 视频=默认播放器)。"""
        try:
            os.startfile(photo.path)      # noqa: S606 (Windows)
        except Exception:
            pass

    def _reveal_file(self):
        if not self.photos:
            return
        _reveal(self.photos[0].path)
